"""test_deploy_verify_primary.py: executes the deploy template's PRIMARY check · LE-459

WHY THIS EXISTS. With a deployment circuit breaker set to roll back, a rollout
whose tasks cannot start ends with the service STABLE on the previous revision.
`aws ecs wait services-stable` returns success on that, so the stability step
alone reports a rolled-back deploy green. _deploy-template.yml's "Verify the
service runs this deploy's image" step is the guard.

Like test_deploy_wait_for_image.py, this does not grep the YAML. It pulls the
step's `run:` block out of the template exactly as GitHub will run it, executes
it under both shells Actions uses with a fake `aws` and a fake `sleep` first on
PATH, and asserts the exit code, the calls made and what a failure says.

Run: python3 -m pytest scripts/test_deploy_verify_primary.py -q
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
STEP_ID = "verify-primary"
REGISTRY = "113831183566.dkr.ecr.us-east-1.amazonaws.com"
NEW = "4aee52e067533960747452babb8bbf399ce20dd2"
OLD = "4b7c8a2dd97a8664d42a6f93f5d4f9e74bad9f20"
TD_NEW = "arn:aws:ecs:us-east-1:113831183566:task-definition/velnor-plane-api:80"
TD_OLD = "arn:aws:ecs:us-east-1:113831183566:task-definition/velnor-plane-api:79"
SIDECAR = "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0"
NEW_IMG = f"{REGISTRY}/velnor-plane-api:{NEW}"
OLD_IMG = f"{REGISTRY}/velnor-plane-api:{OLD}"

SHELLS = {
    "default": ["bash", "-e"],
    "shell-bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}

# FAKE_SPEC drives the fake: td (PRIMARY task definition, "" for none), states
# (rolloutState per successive deployments read; the last one repeats), images
# (the task definition's container images), and optional *_error strings.
FAKE_AWS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, pathlib, sys
    state = pathlib.Path(os.environ["FAKE_STATE"])
    args = sys.argv[1:]
    with open(state / "aws.log", "a") as f:
        f.write(json.dumps(args) + "\\n")
    spec = json.loads(os.environ["FAKE_SPEC"])
    if args[:2] == ["ecs", "describe-services"]:
        q = args[args.index("--query") + 1]
        if "events" in q:
            print("2026-09-13T13:40:00-05:00\\t(service velnor-plane-api) rolling back to deployment ecs-svc/1.")
            sys.exit(0)
        if spec.get("services_error"):
            print(spec["services_error"], file=sys.stderr)
            sys.exit(254)
        n = sum(1 for l in open(state / "aws.log")
                if json.loads(l)[:2] == ["ecs", "describe-services"] and "deployments" in json.loads(l)[json.loads(l).index("--query") + 1])
        states = spec["states"]
        print(f"{spec['td']}\\t{states[min(n, len(states)) - 1]}" if spec["td"] else "None")
        sys.exit(0)
    if args[:2] == ["ecs", "describe-task-definition"]:
        if spec.get("td_error"):
            print(spec["td_error"], file=sys.stderr)
            sys.exit(254)
        print("\\t".join(spec["images"]))
        sys.exit(0)
    print("fake aws: unexpected call " + " ".join(args), file=sys.stderr)
    sys.exit(99)
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


def verify_step() -> dict:
    matches = [s for s in deploy_steps() if s.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected exactly one step with id {STEP_ID}"
    return matches[0]


def step_index(predicate) -> int:
    for i, s in enumerate(deploy_steps()):
        if predicate(s):
            return i
    raise AssertionError("step not found")


def run_verify(tmp_path, shell, spec, image_ref, service="plane-api"):
    step = verify_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
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
        "FAKE_SPEC": json.dumps(spec),
        "IMAGE_REF": image_ref,
        "SERVICE": service,
        "CLUSTER": "velnor-dev",
        "POLL_SECONDS": str(step["env"]["POLL_SECONDS"]),
        "MAX_CHECKS": str(step["env"]["MAX_CHECKS"]),
    }
    proc = subprocess.run(
        [*SHELLS[shell], str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    aws_log = tmp_path / "aws.log"
    sleep_log = tmp_path / "sleep.log"
    calls = [json.loads(l) for l in aws_log.read_text().splitlines()] if aws_log.exists() else []
    sleeps = sleep_log.read_text().split() if sleep_log.exists() else []
    return proc, calls, sleeps


def reads(calls, kind):
    """describe-services calls that read deployments, or task-definition calls."""
    if kind == "deployments":
        return [c for c in calls if c[:2] == ["ecs", "describe-services"] and "deployments" in c[c.index("--query") + 1]]
    if kind == "events":
        return [c for c in calls if c[:2] == ["ecs", "describe-services"] and "events" in c[c.index("--query") + 1]]
    return [c for c in calls if c[:2] == ["ecs", "describe-task-definition"]]


def spec(td=TD_NEW, states=("COMPLETED",), images=(NEW_IMG, SIDECAR), **kw):
    return {"td": td, "states": list(states), "images": list(images), **kw}


# ── Structure: the check runs after stability and before anything reports ────


def test_verify_runs_after_the_stability_wait_and_before_the_smoke_tests():
    wait = step_index(lambda s: s.get("name") == "Wait for ECS service stability")
    verify = step_index(lambda s: s.get("id") == STEP_ID)
    smoke = step_index(lambda s: str(s.get("name", "")).startswith("Smoke test"))
    assert wait < verify < smoke


def test_step_takes_inputs_through_env_only_and_cannot_be_skipped():
    step = verify_step()
    assert step["env"]["IMAGE_REF"] == "${{ inputs.image_tag }}"
    assert step["env"]["SERVICE"] == "${{ inputs.service }}"
    assert step["env"]["CLUSTER"] == "${{ env.ECS_CLUSTER }}"
    assert int(step["env"]["POLL_SECONDS"]) == 10
    assert int(step["env"]["MAX_CHECKS"]) == 12
    assert "${{" not in step["run"], "run: must not interpolate expressions"
    assert "continue-on-error" not in step and "if" not in step


# ── Behaviour ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
def test_new_image_completed_passes_on_first_read(tmp_path, shell):
    proc, calls, sleeps = run_verify(tmp_path, shell, spec(), NEW)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(reads(calls, "deployments")) == 1
    td_calls = reads(calls, "taskdef")
    assert len(td_calls) == 1 and td_calls[0][td_calls[0].index("--task-definition") + 1] == TD_NEW
    assert sleeps == []
    assert "velnor-plane-api:80 is COMPLETED and runs " + NEW_IMG in proc.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_rolled_back_to_previous_revision_fails(tmp_path, shell):
    # The case the step exists for: the circuit breaker rolled back, the old
    # revision is PRIMARY and COMPLETED, and the waiter already said "stable".
    proc, calls, sleeps = run_verify(tmp_path, shell, spec(td=TD_OLD, images=(OLD_IMG, SIDECAR)), NEW)
    assert proc.returncode != 0
    assert "::error title=Service is not running this deploy's image::" in proc.stdout
    assert f"velnor-plane-api:{NEW}" in proc.stdout and OLD_IMG in proc.stdout
    assert "velnor-plane-api:79 (rolloutState=COMPLETED)" in proc.stdout
    assert len(reads(calls, "events")) == 1
    assert sleeps == []


@pytest.mark.parametrize("shell", SHELLS)
def test_rollback_still_in_progress_fails_without_waiting(tmp_path, shell):
    proc, _, sleeps = run_verify(tmp_path, shell, spec(td=TD_OLD, states=("IN_PROGRESS",), images=(OLD_IMG,)), NEW)
    assert proc.returncode != 0
    assert sleeps == []


@pytest.mark.parametrize("shell", SHELLS)
def test_right_image_but_failed_rollout_fails(tmp_path, shell):
    proc, _, sleeps = run_verify(tmp_path, shell, spec(states=("FAILED",)), NEW)
    assert proc.returncode != 0
    assert "(rolloutState=FAILED)" in proc.stdout
    assert sleeps == []


@pytest.mark.parametrize("shell", SHELLS)
def test_right_image_still_converging_is_reread_then_passes(tmp_path, shell):
    proc, calls, sleeps = run_verify(tmp_path, shell, spec(states=("IN_PROGRESS", "IN_PROGRESS", "COMPLETED")), NEW)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(reads(calls, "deployments")) == 3
    assert sleeps == ["10", "10"]


@pytest.mark.parametrize("shell", SHELLS)
def test_right_image_never_completing_fails_at_the_bound(tmp_path, shell):
    proc, calls, sleeps = run_verify(tmp_path, shell, spec(states=("IN_PROGRESS",)), NEW)
    assert proc.returncode != 0
    assert len(reads(calls, "deployments")) == 12
    assert sleeps == ["10"] * 11
    assert "(rolloutState=IN_PROGRESS)" in proc.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_only_a_sidecar_image_is_not_a_match(tmp_path, shell):
    proc, _, _ = run_verify(tmp_path, shell, spec(images=(SIDECAR,)), NEW)
    assert proc.returncode != 0


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "fake,title,needle",
    [
        (spec(services_error="An error occurred (AccessDeniedException) when calling the DescribeServices operation: denied"),
         "Cannot read ECS service", "AccessDeniedException"),
        (spec(td=""), "No PRIMARY deployment", "None"),
        (spec(td_error="An error occurred (ClientException) when calling the DescribeTaskDefinition operation: Unable to describe task definition."),
         "Cannot read task definition", "ClientException"),
    ],
)
def test_unreadable_state_fails_and_says_what_it_is(tmp_path, shell, fake, title, needle):
    proc, _, sleeps = run_verify(tmp_path, shell, fake, NEW)
    assert proc.returncode != 0
    assert f"::error title={title}::" in proc.stdout
    assert needle in proc.stdout
    assert sleeps == []


# ── Which image: the same resolution as "Wait for image in ECR" ──────────────


@pytest.mark.parametrize(
    "service,image_ref,image,ok",
    [
        ("plane-api", NEW, NEW_IMG, True),
        ("admin-api", f"{REGISTRY}/velnor-admin-api:{NEW}", f"{REGISTRY}/velnor-admin-api:{NEW}", True),
        ("chat-api", f"{REGISTRY}/velnor-chat-api@sha256:abc", f"{REGISTRY}/velnor-chat-api@sha256:abc", True),
        # Exact match: a different repository with the same tag is not this image,
        ("plane-api", NEW, f"{REGISTRY}/velnor-plane-api-canary:{NEW}", False),
        # and neither is a shortened tag.
        ("plane-api", NEW, f"{REGISTRY}/velnor-plane-api:{NEW[:7]}", False),
    ],
)
def test_image_resolution_is_exact(tmp_path, service, image_ref, image, ok):
    proc, _, _ = run_verify(tmp_path, "default", spec(images=(image,)), image_ref, service=service)
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
