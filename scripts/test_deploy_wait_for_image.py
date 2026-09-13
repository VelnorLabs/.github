"""test_deploy_wait_for_image.py: executes the deploy template's image wait · LE-459

WHY THIS EXISTS. Every service deploy in the org used to run `tofu apply` and
`ecs update-service` against an image tag that CI had not pushed yet: the
deploy and the build were two workflows on the same push event, with no
`needs:` between them. _deploy-template.yml's "Wait for image in ECR" step is
the guard: nothing in Stage 3 mutates until the exact tag resolves in ECR.

A guard that is only read is a guard nobody has seen bite. So this does not
grep the YAML for a string. It pulls the step's `run:` block out of the
template exactly as GitHub will run it, executes it under the same shells
Actions uses, with a fake `aws` and a fake `sleep` first on PATH, and asserts
what happened: how many ECR calls, how many sleeps and of what length, which
repository and image id were asked for, and the exit code.

The fakes count calls instead of timing them, so "polls for 20 minutes" is
tested in milliseconds and a runaway loop is caught by the subprocess timeout.

Run: python3 -m pytest scripts/test_deploy_wait_for_image.py -q
DEPLOY_TEMPLATE=<path> points it at another copy (used for mutation testing).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = pathlib.Path(
    os.environ.get("DEPLOY_TEMPLATE", ROOT / ".github" / "workflows" / "_deploy-template.yml")
)
STEP_ID = "wait-for-image"
REGISTRY = "113831183566.dkr.ecr.us-east-1.amazonaws.com"
SHA = "fa0b1247b71e88d0c55b3f1bdceeb3fdf12e2822"

# Actions runs a `run:` block with `bash -e {0}` when no shell is given, and
# with `bash --noprofile --norc -eo pipefail {0}` for `shell: bash`. The step
# must behave identically under both, so every behavioural case runs twice.
SHELLS = {
    "default": ["bash", "-e"],
    "shell-bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}

FAKE_AWS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, pathlib, sys
    state = pathlib.Path(os.environ["FAKE_STATE"])
    with open(state / "aws.log", "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    if sys.argv[1:3] != ["ecr", "describe-images"]:
        print("fake aws: unexpected call " + " ".join(sys.argv[1:]), file=sys.stderr)
        sys.exit(99)
    n = sum(1 for _ in open(state / "aws.log"))
    mode = os.environ["FAKE_AWS_MODE"]
    err = "An error occurred ({}) when calling the DescribeImages operation: {}"
    def not_found():
        print(err.format("ImageNotFoundException", "The image with imageId does not exist within the repository"), file=sys.stderr)
        sys.exit(254)
    if mode == "present":
        print("2026-09-13T10:25:01.529000-05:00"); sys.exit(0)
    if mode.startswith("late:"):
        if n <= int(mode.split(":")[1]):
            not_found()
        print("2026-09-13T10:25:01.529000-05:00"); sys.exit(0)
    if mode == "never":
        not_found()
    if mode == "denied":
        print(err.format("AccessDeniedException", "User: arn:aws:sts::1:assumed-role/velnor-github-deploy/x is not authorized to perform: ecr:DescribeImages"), file=sys.stderr)
        sys.exit(254)
    if mode == "norepo":
        print(err.format("RepositoryNotFoundException", "The repository with name 'velnor-x' does not exist"), file=sys.stderr)
        sys.exit(254)
    sys.exit(98)
    """
)

FAKE_SLEEP = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    echo "$1" >> "$FAKE_STATE/sleep.log"
    """
)


def load_template() -> dict:
    return yaml.safe_load(TEMPLATE.read_text())


def deploy_steps() -> list[dict]:
    return load_template()["jobs"]["deploy-dev"]["steps"]


def wait_step() -> dict:
    matches = [s for s in deploy_steps() if s.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected exactly one step with id {STEP_ID}"
    return matches[0]


def step_index(predicate) -> int:
    for i, s in enumerate(deploy_steps()):
        if predicate(s):
            return i
    raise AssertionError("step not found")


def run_wait(tmp_path, shell, mode, image_ref, service="agent-gateway", minutes="20"):
    step = wait_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Absolute interpreter, not `env python3`: a version-manager shim for
    # python3 can need $HOME and would fail before the fake ever ran.
    (bin_dir / "aws").write_text(FAKE_AWS.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    (bin_dir / "sleep").write_text(FAKE_SLEEP)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_STATE": str(tmp_path),
        "FAKE_AWS_MODE": mode,
        "IMAGE_REF": image_ref,
        "SERVICE": service,
        "WAIT_MINUTES": minutes,
        "POLL_SECONDS": str(step["env"]["POLL_SECONDS"]),
    }
    proc = subprocess.run(
        [*SHELLS[shell], str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    aws_log = tmp_path / "aws.log"
    sleep_log = tmp_path / "sleep.log"
    calls = [json.loads(l) for l in aws_log.read_text().splitlines()] if aws_log.exists() else []
    sleeps = sleep_log.read_text().split() if sleep_log.exists() else []
    return proc, calls, sleeps


def arg(call: list[str], flag: str) -> str:
    return call[call.index(flag) + 1]


# ── Structure: the guard sits where it can still prevent the mutation ────────


def test_wait_runs_after_credentials_and_before_any_mutation():
    wait = step_index(lambda s: s.get("id") == STEP_ID)
    creds = step_index(lambda s: s.get("name") == "Configure AWS credentials via OIDC")
    tofu_init = step_index(lambda s: str(s.get("name", "")).startswith("tofu init"))
    tofu_apply = step_index(lambda s: str(s.get("name", "")).startswith("tofu apply"))
    force = step_index(lambda s: s.get("name") == "Force new ECS deployment")
    assert creds < wait < tofu_init < tofu_apply < force


def test_step_takes_inputs_through_env_only():
    step = wait_step()
    assert step["env"]["IMAGE_REF"] == "${{ inputs.image_tag }}"
    assert step["env"]["SERVICE"] == "${{ inputs.service }}"
    assert step["env"]["WAIT_MINUTES"] == "${{ inputs.image_wait_minutes }}"
    assert "${{" not in step["run"], "run: must not interpolate expressions"
    assert "continue-on-error" not in step and "if" not in step


def test_bound_is_an_input_with_a_positive_default():
    spec = load_template()[True]["workflow_call"]["inputs"]["image_wait_minutes"]
    assert spec["type"] == "number"
    assert spec["default"] == 20
    assert int(wait_step()["env"]["POLL_SECONDS"]) == 10


# ── Behaviour ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
def test_present_image_proceeds_on_first_call_without_sleeping(tmp_path, shell):
    proc, calls, sleeps = run_wait(tmp_path, shell, "present", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 1
    assert sleeps == []
    assert arg(calls[0], "--repository-name") == "velnor-agent-gateway"
    assert arg(calls[0], "--image-ids") == f"imageTag={SHA}"


@pytest.mark.parametrize("shell", SHELLS)
def test_late_image_proceeds_after_polling(tmp_path, shell):
    proc, calls, sleeps = run_wait(tmp_path, shell, "late:3", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 4
    assert sleeps == ["10", "10", "10"]
    assert all(arg(c, "--image-ids") == f"imageTag={SHA}" for c in calls)
    assert "not in ECR yet" in proc.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_image_that_never_appears_fails_loudly_at_the_bound(tmp_path, shell):
    proc, calls, sleeps = run_wait(tmp_path, shell, "never", SHA, minutes="1")
    assert proc.returncode != 0
    # 1 minute at 10s = 6 checks and 5 sleeps between them, not one more.
    assert len(calls) == 6
    assert sleeps == ["10"] * 5
    assert "::error title=Image never appeared in ECR::" in proc.stdout
    assert SHA in proc.stdout


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("mode,needle", [("denied", "AccessDeniedException"), ("norepo", "RepositoryNotFoundException")])
def test_non_absence_errors_fail_immediately_and_say_what_they_are(tmp_path, shell, mode, needle):
    proc, calls, sleeps = run_wait(tmp_path, shell, mode, SHA)
    assert proc.returncode != 0
    assert len(calls) == 1
    assert sleeps == []
    assert "::error title=Cannot read ECR::" in proc.stdout
    assert needle in proc.stdout
    assert "never appeared" not in proc.stdout


# ── Which image: repository and id come from image_tag, not from a guess ─────


@pytest.mark.parametrize(
    "service,image_ref,repo,image_id",
    [
        ("plane-api", SHA, "velnor-plane-api", f"imageTag={SHA}"),
        ("admin-api", f"{REGISTRY}/velnor-admin-api:{SHA}", "velnor-admin-api", f"imageTag={SHA}"),
        # The URI's repository wins over velnor-<service>.
        ("chat-api", f"{REGISTRY}/velnor-chat-api-canary:t1", "velnor-chat-api-canary", "imageTag=t1"),
        ("chat-api", f"{REGISTRY}/velnor-chat-api@sha256:abc", "velnor-chat-api", "imageDigest=sha256:abc"),
        ("chat-api", f"{REGISTRY}/velnor-chat-api:t1@sha256:abc", "velnor-chat-api", "imageDigest=sha256:abc"),
    ],
)
def test_repository_and_image_id_resolution(tmp_path, service, image_ref, repo, image_id):
    proc, calls, _ = run_wait(tmp_path, "default", "present", image_ref, service=service)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert arg(calls[0], "--repository-name") == repo
    assert arg(calls[0], "--image-ids") == image_id
