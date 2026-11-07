"""Failure-path tests for the production controller and executable adapters."""
import copy
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

import adapter
import pipeline as p

SHA = "a" * 40
HASH = "b" * 64
ACCOUNT = "123456789012"
REGION = "us-east-1"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/velnor-test@sha256:{HASH}"


@pytest.fixture(autouse=True)
def no_cloud_manifest_upload(monkeypatch):
    monkeypatch.setattr(p, "publish_release", lambda *a: {"VELNOR_RELEASE_KEY": "manifest.json", "VELNOR_RELEASE_VERSION": "v1"})


@pytest.fixture
def draft():
    return p.read_json(p.ROOT / "production/releases/candidate.json"), p.read_json(p.ROOT / "production/targets/production.json")


@pytest.fixture
def ready(draft):
    r, t = copy.deepcopy(draft)
    r["state"] = "ready"
    t.update(account_id=ACCOUNT, region=REGION, receipt_bucket="velnor-prod-receipts", runner_image=IMAGE, receipt_kms_key_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/abc-def")
    for c in r["components"].values():
        c.update(source_sha=SHA, ci_run_id=1, ci_workflow=".github/workflows/ci.yml", artifacts=[])
    for name, b in t["projects"].items():
        b["name"] = "velnor-prod-" + name
        stage = b["stage"]
        task = {"task_definition": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/velnor-test:12",
                "cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/velnor-prod-cluster", "images": {"main": IMAGE}}
        if stage.startswith("infra-"):
            op = {"tofu_version": "1.12.0", "workspaces": [{"path": "accounts/prod/ecs/test", "variables": {}}]}
        elif stage == "backend":
            op = {**task, "kind": "ecs-service", "service": "velnor-test"}
        elif stage == "web":
            op = {"environment": "production", "destination_bucket": "velnor-prod-web", "distribution_id": "E12345", "destination_kms_key_arn": t["receipt_kms_key_arn"],
                  "artifact": {"bucket": "velnor-prod-artifacts", "key": "web.tar.gz", "version_id": "v1", "sha256": HASH},
                  "build_metadata": {"environment": "production", "source_sha": SHA, "config_sha256": HASH}}
        else:
            op = {**task, "container": "main", "subnets": ["subnet-abc123"], "security_groups": ["sg-abc123"]}
            if stage in ("migration-check", "migrate"):
                op["migration_set_sha256"] = HASH
        r["operations"][name] = op
    return r, t


def test_draft_previews_but_cannot_execute(draft):
    r, t = draft
    assert "UNPINNED" in p.preview(r, t)
    with pytest.raises(p.GateError, match="remains draft"):
        p.validate(r, t, ready=True)


def test_complete_manifest_validates(ready):
    assert p.validate(*ready, ready=True) == []


def test_infrastructure_can_run_before_application_pins(ready):
    r, t = ready
    r["components"]["web"].update(source_sha=None, ci_run_id=None, artifacts=None)
    r["operations"]["web"] = None
    t["projects"]["web"]["name"] = None
    assert p.validate(r, t, ready=True, scope="infra-plan") == []
    with pytest.raises(p.GateError, match="unresolved"):
        p.validate(r, t, ready=True)


@pytest.mark.parametrize("mutation", [
    lambda r, t: r["components"]["iac"].update(source_sha="main"),
    lambda r, t: r["components"]["iac"].update(source_sha="a" * 7),
    lambda r, t: r["components"]["iac"].update(ci_run_id=None),
    lambda r, t: r["components"]["iac"].update(artifacts=[{"kind": "ecr", "uri": IMAGE.split("@")[0] + ":latest"}]),
    lambda r, t: t.update(environment="dev"),
    lambda r, t: t.update(runner_image="aws/codebuild/standard:latest"),
    lambda r, t: t["projects"]["migrate"].update(name="velnor-dev-migrate"),
    lambda r, t: r["stages"].update(migrate=[]),
    lambda r, t: r["operations"].update(migrate=None),
    lambda r, t: r["operations"]["migrate"].update(migration_set_sha256="c" * 64),
    lambda r, t: r["operations"]["migrate"].update(task_definition=f"arn:aws:ecs:{REGION}:999999999999:task-definition/migrate:1"),
    lambda r, t: r["operations"]["infra-apply"].update(tofu_version="1.9.0"),
    lambda r, t: r["operations"]["web"].update(environment="development"),
    lambda r, t: r["operations"]["web"]["artifact"].update(version_id="null"),
])
def test_invalid_manifest_blocks_before_aws(ready, mutation):
    r, t = ready
    mutation(r, t)
    with pytest.raises(p.GateError):
        p.validate(r, t, ready=True)


def test_paths_cannot_escape_release_directory():
    with pytest.raises(p.GateError):
        p.checked_file("production/releases/../../README.md", "production/releases")


def test_duplicate_json_keys_rejected(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"state":"draft","state":"ready"}')
    with pytest.raises(p.GateError, match="duplicate"):
        p.read_json(f)


@pytest.mark.parametrize("bad", [{"conclusion": "failure"}, {"head_sha": "c" * 40},
    {"path": ".github/workflows/unrelated.yml"}, {"event": "pull_request"}, {"head_branch": "feature/test"}])
def test_ci_must_match_exact_release(ready, monkeypatch, bad):
    r, _ = ready
    def response(path):
        repo = path.split("/repos/")[1].split("/actions/")[0]
        return {"repository": {"full_name": repo}, "head_sha": SHA, "status": "completed", "conclusion": "success",
                "path": ".github/workflows/ci.yml", "event": "push", "head_branch": "main", **bad}
    monkeypatch.setattr(p, "github", response)
    with pytest.raises(p.GateError):
        p.verify_ci(r)


def project(binding, component, targets):
    return {"arn": f"arn:aws:codebuild:{REGION}:{ACCOUNT}:project/" + binding["name"],
            "tags": [{"key": "Environment", "value": "production"}],
            "source": {"type": "GITHUB", "location": "https://github.com/" + component["repository"], "buildspec": binding["buildspec"]},
            "secondarySources": [{"type": "GITHUB", "sourceIdentifier": "controller", "location": "https://github.com/VelnorLabs/.github"}],
            "environment": {"image": IMAGE},
            "serviceRole": f"arn:aws:iam::{ACCOUNT}:role/velnor-prod-build",
            "artifacts": {"type": "S3", "location": targets["receipt_bucket"], "packaging": "NONE"}}


@pytest.mark.parametrize("change", [
    lambda x: x["source"].update(location="https://github.com/other/repo"),
    lambda x: x["source"].update(buildspec="unexpected.yml"),
    lambda x: x.update(tags=[]),
    lambda x: x.update(secondarySources=[]),
    lambda x: x["environment"].update(image="mutable:latest"),
    lambda x: x["artifacts"].update(encryptionDisabled=True),
])
def test_wrong_project_rejected(ready, monkeypatch, change):
    r, t = ready
    b = t["projects"]["migrate"]
    c = r["components"][b["component"]]
    value = project(b, c, t)
    change(value)
    monkeypatch.setattr(p, "aws", lambda *a, **k: {"projects": [value]})
    with pytest.raises(p.GateError):
        p.verify_project(b, c, t)


@pytest.mark.parametrize("status,receipt_error", [("FAILED", False), ("SUCCEEDED", True), ("SUCCEEDED", False)])
def test_stage_waits_and_verifies_receipt(ready, monkeypatch, status, receipt_error):
    r, t = ready
    calls = []
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setattr(p, "verify_project", lambda *a: None)
    statuses = iter(["IN_PROGRESS", status])
    def fake(service, action, **kwargs):
        calls.append((action, kwargs))
        if action == "start-build":
            assert kwargs["sourceVersion"] == SHA
            assert kwargs["secondarySourcesVersionOverride"][0]["sourceVersion"] == SHA
            assert "adapter.py" in kwargs["buildspecOverride"]
            return {"build": {"id": "project:uuid"}}
        if action == "batch-get-builds":
            return {"builds": [{"id": "project:uuid", "buildStatus": next(statuses), "resolvedSourceVersion": SHA}]}
        return {}
    def receipt(build, expected, target):
        if receipt_error:
            raise p.GateError("receipt mismatch: release_sha256")
        return {**expected, "destructive_changes": False, "evidence": {"checked": True}}, {"key": "receipt"}
    monkeypatch.setattr(p, "aws", fake)
    monkeypatch.setattr(p, "load_receipt", receipt)
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    if status != "SUCCEEDED" or receipt_error:
        with pytest.raises(p.GateError):
            p.execute_stage("migration-check", r, t, "123-1")
    else:
        assert len(p.execute_stage("migration-check", r, t, "123-1")) == 1
    assert sum(x[0] == "batch-get-builds" for x in calls) == 2


def test_timeout_stops_remote_build(ready, monkeypatch):
    r, t = ready
    calls = []
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setattr(p, "verify_project", lambda *a: None)
    def fake(service, action, **kwargs):
        calls.append(action)
        if action == "start-build":
            return {"build": {"id": "project:uuid"}}
        if action == "batch-get-builds":
            return {"builds": [{"buildStatus": "IN_PROGRESS"}]}
        return {}
    monkeypatch.setattr(p, "aws", fake)
    with pytest.raises(p.GateError, match="timed out"):
        p.execute_stage("migration-check", r, t, "123-1", timeout=-1)
    assert calls[-1] == "stop-build"


def test_green_build_cannot_claim_destructive_check(ready, monkeypatch):
    r, t = ready
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setattr(p, "verify_project", lambda *a: None)
    monkeypatch.setattr(p, "aws", lambda _, action, **kw: {"build": {"id": "x"}} if action == "start-build" else
                        {"builds": [{"buildStatus": "SUCCEEDED", "resolvedSourceVersion": SHA}]})
    monkeypatch.setattr(p, "load_receipt", lambda *a: ({"destructive_changes": True}, {}))
    with pytest.raises(p.GateError, match="destructive"):
        p.execute_stage("migration-check", r, t, "123-1")


def test_ecs_stability_on_old_revision_is_failure(ready, monkeypatch):
    r, _ = ready
    op = r["operations"]["plane-api"]
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("VELNOR_PRODUCTION_ACCOUNT", ACCOUNT)
    monkeypatch.setattr(adapter, "record", lambda *a: None)
    monkeypatch.setattr(adapter, "protect_images", lambda *a: None)
    calls = []
    def fake(api, action, **kw):
        calls.append(action)
        if action == "describe-task-definition":
            return {"taskDefinition": {"containerDefinitions": [{"name": "main", "image": IMAGE}]}}
        if action == "describe-services":
            return {"services": [{"taskDefinition": "old:1", "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
                                   "deployments": [{"status": "PRIMARY", "taskDefinition": "old:1", "rolloutState": "COMPLETED"}]}]}
        return {}
    monkeypatch.setattr(adapter, "aws", fake)
    with pytest.raises(RuntimeError, match="rolled back"):
        adapter.backend(op)
    assert "update-service" in calls


def test_task_image_mismatch_blocks_update():
    with pytest.raises(RuntimeError, match="containers"):
        adapter.verify_images({"containerDefinitions": [{"name": "main", "image": "wrong:latest"}]}, {"main": IMAGE})


def test_receipt_cannot_be_emitted_after_failed_build(tmp_path):
    result = subprocess.run([sys.executable, str(p.ROOT / "scripts/production/receipt.py"), "--evidence", "unused.json"],
                            cwd=tmp_path, env={**os.environ, "CODEBUILD_BUILD_SUCCEEDING": "0"}, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / "production-receipt.json").exists()


@pytest.mark.parametrize("wrong", ["release_sha256", "source_sha", "controller_sha", "stage", "status", "kms", "version"])
def test_receipt_cannot_be_reused_or_substituted(ready, monkeypatch, wrong):
    _, t = ready
    expected = {"release_sha256": HASH, "source_sha": SHA, "controller_sha": SHA, "stage": "migrate"}
    receipt = {**expected, "status": "success", "evidence": {"migrated": True}}
    if wrong not in ("kms", "version"):
        receipt[wrong] = "incorrect"
    monkeypatch.setattr(p, "aws", lambda *a, **k: {
        "VersionId": "null" if wrong == "version" else "v1", "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": "wrong" if wrong == "kms" else t["receipt_kms_key_arn"], "ContentLength": 100})
    def fetch(args, **kwargs):
        from pathlib import Path
        Path(args[-1]).write_text(json.dumps(receipt))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(p.subprocess, "run", fetch)
    with pytest.raises(p.GateError):
        p.load_receipt({"artifacts": {"location": "arn:aws:s3:::velnor-prod-receipts/run/artifacts"}}, expected, t)


def test_orphaned_codebuild_run_blocks_new_release(ready, monkeypatch):
    _, t = ready
    monkeypatch.setattr(p, "aws", lambda _, action, **kw:
                        {"ids": ["previous:build"]} if action == "list-builds-for-project" else
                        {"builds": [{"buildStatus": "IN_PROGRESS"}]})
    with pytest.raises(p.GateError, match="still running"):
        p.ensure_idle(t)


@pytest.mark.parametrize("resource,actions,before,after", [
    ("aws_s3_bucket", ["delete"], {"bucket": "x"}, None),
    ("aws_ecs_service", ["update"], {"task_definition": "old:1"}, {"task_definition": "new:2"}),
    ("aws_lambda_alias", ["update"], {"function_version": "1"}, {"function_version": "2"}),
])
def test_infrastructure_cannot_bypass_migration_gate(resource, actions, before, after):
    with pytest.raises(RuntimeError):
        adapter.check_plan({"resource_changes": [{"type": resource, "change": {"actions": actions, "before": before, "after": after}}]})


def test_infrastructure_allows_additive_foundation():
    adapter.check_plan({"resource_changes": [{"type": "aws_s3_bucket", "change": {"actions": ["create"], "before": None, "after": {"bucket": "new"}}}]})


@pytest.mark.parametrize("env", [{"PRODUCTION_PIPELINES_ENABLED": "false"}, {"GITHUB_REF": "refs/heads/feature"},
    {"GITHUB_EVENT_NAME": "push"}, {"PRODUCTION_ROLE_ARN": "arn:aws:iam::999999999999:role/velnor-prod-controller"}])
def test_oidc_rejects_inactive_wrong_branch_or_wrong_account(monkeypatch, env):
    import oidc
    monkeypatch.setattr(oidc, "_expires", 0)
    base = {"PRODUCTION_ACCOUNT_ID": ACCOUNT, "PRODUCTION_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/velnor-prod-controller",
            "PRODUCTION_PIPELINES_ENABLED": "true", "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(oidc, "urlopen", lambda *a, **k: pytest.fail("must fail before exchanging credentials"))
    with pytest.raises(RuntimeError):
        oidc.refresh()


def test_production_workflows_share_lock_and_have_no_actions():
    for filename in ("promote-to-production.yml", "production-infrastructure.yml"):
        doc = yaml.safe_load((p.ROOT / ".github/workflows" / filename).read_text())
        assert set(doc.get("on", doc.get(True))) == {"workflow_dispatch"}
        assert doc["concurrency"] == {"group": "velnor-production", "cancel-in-progress": False}
        assert doc["jobs"]["execute"]["environment"] == "production"
        assert doc["jobs"]["execute"]["needs"] == "preview"
        assert "id-token" not in doc.get("permissions", {})
        assert all("uses" not in s for j in doc["jobs"].values() for s in j["steps"])
