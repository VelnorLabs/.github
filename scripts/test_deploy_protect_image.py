"""test_deploy_protect_image.py: executes the deploy template's image protection

WHY THIS EXISTS. On 2026-10-24 velnor-plane-api and velnor-plane-setup could not
start a task (CannotPullContainerError): ECR's catch-all lifecycle rule had
expired the images they were running. velnor-iac accounts/dev/ecr now has a
rule that owns every image tagged deployed-*, and _deploy-template.yml's
"Protect the deployed image from ECR lifecycle expiry" step puts that tag on
the exact digest each deploy is about to run.

Like test_deploy_wait_for_image.py, this does not grep the YAML. It pulls the
step's `run:` block out of the template exactly as GitHub will run it, executes
it under both shells Actions uses with a fake `aws` first on PATH, and asserts
the exit code, the calls made, the exact manifest bytes written back, and what
a failure says.

Run: python3 -m pytest scripts/test_deploy_protect_image.py -q
DEPLOY_TEMPLATE=<path> points it at another copy (used for mutation testing).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = pathlib.Path(
    os.environ.get("DEPLOY_TEMPLATE", ROOT / ".github" / "workflows" / "_deploy-template.yml")
)
STEP_ID = "protect-image"
REGISTRY = "113831183566.dkr.ecr.us-east-1.amazonaws.com"
SHA = "3a798f10f6ff467950abbce046eddedd452b5ea5"
DIGEST = "sha256:" + "1" * 64
# A manifest WITH a trailing newline and indentation: the bytes written back
# must be identical, or put-image would hash to a different digest.
MANIFEST = '{\n   "schemaVersion": 2,\n   "mediaType": "application/vnd.docker.distribution.manifest.v2+json"\n}\n'
MEDIA = "application/vnd.docker.distribution.manifest.v2+json"

SHELLS = {
    "default": ["bash", "-e"],
    "shell-bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}

FAKE_AWS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, pathlib, shutil, sys
    state = pathlib.Path(os.environ["FAKE_STATE"])
    argv = sys.argv[1:]
    with open(state / "aws.log", "a") as f:
        f.write(json.dumps(argv) + "\\n")
    def arg(flag):
        return argv[argv.index(flag) + 1]
    cfg = json.loads(os.environ["FAKE_CFG"])
    def err(code, op):
        print(f"An error occurred ({code}) when calling the {op} operation: x", file=sys.stderr)
        sys.exit(254)
    if argv[:2] == ["ecr", "describe-images"]:
        image_id = arg("--image-ids")
        if image_id.startswith("imageTag=deployed-"):
            if cfg.get("readback_fail"):
                err("ImageNotFoundException", "DescribeImages")
            print(cfg.get("readback", cfg["digest"])); sys.exit(0)
        if cfg.get("describe") == "denied":
            err("AccessDeniedException", "DescribeImages")
        print(json.dumps({"imageDigest": cfg["digest"], "imageTags": cfg["tags"]})); sys.exit(0)
    if argv[:2] == ["ecr", "batch-get-image"]:
        if cfg.get("bgi") == "denied":
            err("AccessDeniedException", "BatchGetImage")
        print(json.dumps({"images": [{"imageId": {"imageDigest": cfg["digest"]},
                                      "imageManifest": cfg["manifest"],
                                      "imageManifestMediaType": cfg["media"]}],
                          "failures": []})); sys.exit(0)
    if argv[:2] == ["ecr", "put-image"]:
        shutil.copy(arg("--image-manifest")[len("file://"):], state / "sent-manifest")
        put = cfg.get("put", "ok")
        if put == "exists":
            err("ImageAlreadyExistsException", "PutImage")
        if put == "collide":
            err("ImageTagAlreadyExistsException", "PutImage")
        if put == "denied":
            err("AccessDeniedException", "PutImage")
        print(json.dumps({"image": {"imageId": {"imageDigest": cfg["digest"]}}})); sys.exit(0)
    print("fake aws: unexpected call " + " ".join(argv), file=sys.stderr)
    sys.exit(99)
    """
)


def deploy_steps() -> list[dict]:
    return yaml.safe_load(TEMPLATE.read_text())["jobs"]["deploy-dev"]["steps"]


def protect_step() -> dict:
    matches = [s for s in deploy_steps() if s.get("id") == STEP_ID]
    assert len(matches) == 1, f"expected exactly one step with id {STEP_ID}"
    return matches[0]


def step_index(predicate) -> int:
    for i, s in enumerate(deploy_steps()):
        if predicate(s):
            return i
    raise AssertionError("step not found")


def run_protect(tmp_path, shell, image_ref=SHA, service="plane-api", **cfg):
    conf = {"digest": DIGEST, "tags": [SHA], "manifest": MANIFEST, "media": MEDIA, **cfg}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(FAKE_AWS.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    (bin_dir / "aws").chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(protect_step()["run"])
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_STATE": str(tmp_path),
        "FAKE_CFG": json.dumps(conf),
        "IMAGE_REF": image_ref,
        "SERVICE": service,
    }
    proc = subprocess.run(
        [*SHELLS[shell], str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    log = tmp_path / "aws.log"
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return proc, calls


def arg(call: list[str], flag: str) -> str:
    return call[call.index(flag) + 1]


def puts(calls):
    return [c for c in calls if c[:2] == ["ecr", "put-image"]]


# ── Structure ────────────────────────────────────────────────────────────────


def test_jq_is_available():
    assert shutil.which("jq"), "the step uses jq (preinstalled on ubuntu-latest)"


def test_protect_runs_after_the_wait_and_before_any_mutation():
    wait = step_index(lambda s: s.get("id") == "wait-for-image")
    protect = step_index(lambda s: s.get("id") == STEP_ID)
    tofu_init = step_index(lambda s: str(s.get("name", "")).startswith("tofu init"))
    tofu_apply = step_index(lambda s: str(s.get("name", "")).startswith("tofu apply"))
    force = step_index(lambda s: s.get("name") == "Force new ECS deployment")
    assert wait < protect < tofu_init < tofu_apply < force


def test_step_takes_inputs_through_env_only_and_is_fail_closed():
    step = protect_step()
    assert step["env"]["IMAGE_REF"] == "${{ inputs.image_tag }}"
    assert step["env"]["SERVICE"] == "${{ inputs.service }}"
    assert "${{" not in step["run"], "run: must not interpolate expressions"
    assert "continue-on-error" not in step and "if" not in step


# ── Behaviour ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shell", SHELLS)
def test_tags_the_exact_digest_deployed_sha_with_byte_exact_manifest(tmp_path, shell):
    proc, calls = run_protect(tmp_path, shell)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    [put] = puts(calls)
    assert arg(put, "--repository-name") == "velnor-plane-api"
    assert arg(put, "--image-tag") == f"deployed-{SHA}"
    assert arg(put, "--image-digest") == DIGEST
    assert arg(put, "--image-manifest-media-type") == MEDIA
    assert (tmp_path / "sent-manifest").read_text() == MANIFEST
    readback = calls[-1]
    assert readback[:2] == ["ecr", "describe-images"]
    assert arg(readback, "--image-ids") == f"imageTag=deployed-{SHA}"
    assert f"Protected velnor-plane-api {DIGEST} as deployed-{SHA}." in proc.stdout


@pytest.mark.parametrize(
    "service,image_ref,repo,image_id",
    [
        ("plane-api", SHA, "velnor-plane-api", f"imageTag={SHA}"),
        ("admin-api", f"{REGISTRY}/velnor-admin-api:{SHA}", "velnor-admin-api", f"imageTag={SHA}"),
        ("chat-api", f"{REGISTRY}/velnor-chat-api@{DIGEST}", "velnor-chat-api", f"imageDigest={DIGEST}"),
        ("chat-api", f"{REGISTRY}/velnor-chat-api:t1@{DIGEST}", "velnor-chat-api", f"imageDigest={DIGEST}"),
    ],
)
def test_repository_and_image_id_resolution_matches_the_wait(tmp_path, service, image_ref, repo, image_id):
    proc, calls = run_protect(tmp_path, "default", image_ref=image_ref, service=service)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert arg(calls[0], "--repository-name") == repo
    assert arg(calls[0], "--image-ids") == image_id
    assert arg(puts(calls)[0], "--image-tag") == f"deployed-{SHA}"


@pytest.mark.parametrize("tags", [["custom"], [SHA, "a" * 40], []])
def test_without_exactly_one_git_sha_tag_the_digest_names_it(tmp_path, tags):
    proc, calls = run_protect(tmp_path, "default", image_ref=f"velnor-x@{DIGEST}", tags=tags)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert arg(puts(calls)[0], "--image-tag") == "deployed-" + "1" * 64


@pytest.mark.parametrize("tags", [[SHA, f"deployed-{SHA}"], [f"migrate-{SHA}"]])
def test_already_protected_image_is_not_rewritten(tmp_path, tags):
    proc, calls = run_protect(tmp_path, "default", tags=tags)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert puts(calls) == []
    assert "already protected" in proc.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_tag_already_on_this_digest_is_success(tmp_path, shell):
    proc, _ = run_protect(tmp_path, shell, put="exists")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "cfg,title",
    [
        ({"describe": "denied"}, "Cannot read ECR"),
        ({"bgi": "denied"}, "Cannot read the image manifest"),
        ({"put": "denied"}, "Cannot tag the deployed image"),
        ({"put": "collide"}, "deployed tag collision"),
        ({"readback": "sha256:" + "2" * 64}, "deployed tag did not resolve"),
        ({"readback_fail": True}, "deployed tag did not resolve"),
    ],
)
def test_failures_stop_the_deploy_and_say_what_they_are(tmp_path, shell, cfg, title):
    proc, _ = run_protect(tmp_path, shell, **cfg)
    assert proc.returncode != 0
    assert f"::error title={title}::" in proc.stdout
    assert "Not deploying" in proc.stdout
    assert "Protected " not in proc.stdout


def test_unusable_image_ref_fails(tmp_path):
    proc, calls = run_protect(tmp_path, "default", image_ref=f"{REGISTRY}/velnor-x")
    assert proc.returncode != 0
    assert "::error title=Unusable image_tag::" in proc.stdout
    assert calls == []
