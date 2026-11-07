#!/usr/bin/env python3
"""Production promotion controller. Standard library only; never resolves 'latest'.

AWS work executes in explicitly configured production CodeBuild projects, where
private network access and per-stage IAM belong. A green build is insufficient:
every stage must return a release-bound receipt from its versioned S3 artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
STAGES = ("migration-check", "migrate", "backend", "web", "verify")
ALL_STAGES = ("infra-plan", "infra-apply", *STAGES)
SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"[0-9a-f]{64}")
REPO = re.compile(r"VelnorLabs/(?:velnor-[a-z0-9-]+|\.github)")
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")


class GateError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise GateError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def checked_file(path, directory):
    target = (ROOT / path).resolve()
    require(target.is_relative_to(ROOT / directory), f"path must be inside {directory}")
    require(target.suffix == ".json" and target.is_file(), "expected an existing JSON file")
    return target


def validate_operation(stage, op, targets):
    require(isinstance(op, dict), "operation must be an object")
    account, region = targets.get("account_id"), targets.get("region")

    def task():
        require(re.fullmatch(r"arn:aws:ecs:[a-z0-9-]+:\d{12}:task-definition/[a-zA-Z0-9_-]+:\d+", op.get("task_definition", "")),
                "pinned task-definition ARN required")
        require(op["task_definition"].startswith(f"arn:aws:ecs:{region}:{account}:"), "task belongs to another account/region")
        require(op.get("cluster", "").startswith(f"arn:aws:ecs:{region}:{account}:cluster/velnor-prod"), "production cluster ARN required")
        require(isinstance(op.get("images"), dict) and op["images"], "all container images must be pinned")
        for image in op["images"].values():
            require(re.fullmatch(rf"{account}\.dkr\.ecr\.{region}\.amazonaws\.com/[a-z0-9/_-]+@sha256:[0-9a-f]{{64}}", image),
                    "task image must be a digest in production ECR")

    if stage in ("infra-plan", "infra-apply"):
        require(re.fullmatch(r"\d+\.\d+\.\d+", op.get("tofu_version", "")), "OpenTofu version must be pinned")
        require(isinstance(op.get("workspaces"), list) and op["workspaces"], "explicit infrastructure workspaces required")
        paths = []
        for w in op["workspaces"]:
            require(re.fullmatch(r"accounts/prod/[a-zA-Z0-9_/-]+", w.get("path", "")) and ".." not in w["path"], "only accounts/prod workspaces allowed")
            require(isinstance(w.get("variables"), dict), "explicit infrastructure variables required")
            paths.append(w["path"])
        require(len(set(paths)) == len(paths), "duplicate infrastructure workspace")
    elif stage == "backend" and op.get("kind") == "lambda-alias":
        require(op.get("function", "").startswith(f"arn:aws:lambda:{region}:{account}:function:velnor-prod-"), "production Lambda ARN required")
        require(re.fullmatch(r"[1-9][0-9]*", op.get("version", "")) and op.get("alias") == "production", "published version/production alias required")
        require(re.fullmatch(r"[A-Za-z0-9+/]{43}=", op.get("code_sha256", "")), "Lambda CodeSha256 required")
    elif stage == "backend":
        require(op.get("kind") == "ecs-service", "backend kind must be ecs-service or lambda-alias")
        task()
        require(re.fullmatch(r"velnor-[a-z0-9-]+", op.get("service", "")), "explicit ECS service required")
    elif stage == "web":
        require(op.get("environment") == "production", "production web bundle required")
        require(re.fullmatch(r"velnor-prod-[a-z0-9.-]+", op.get("destination_bucket", "")), "production Web bucket required")
        require(re.fullmatch(rf"arn:aws:kms:{region}:{account}:key/[a-f0-9-]+", op.get("destination_kms_key_arn", "")), "production Web KMS key required")
        require(re.fullmatch(r"[A-Z0-9]+", op.get("distribution_id", "")), "CloudFront distribution ID required")
        a = op.get("artifact", {})
        require(a.get("bucket") and a.get("key") and a.get("version_id") not in (None, "", "null") and HASH.fullmatch(a.get("sha256", "")),
                "Web artifact must pin S3 version and checksum")
        metadata = op.get("build_metadata", {})
        require(metadata.get("environment") == "production" and SHA.fullmatch(metadata.get("source_sha", "")) and
                HASH.fullmatch(metadata.get("config_sha256", "")), "Web requires source and public configuration provenance")
    else:
        task()
        require(op.get("container") in op["images"], "task container required")
        for field, pattern in (("subnets", r"subnet-[0-9a-f]+"), ("security_groups", r"sg-[0-9a-f]+")):
            require(isinstance(op.get(field), list) and op[field] and all(re.fullmatch(pattern, x) for x in op[field]), "explicit private task network required")
        if stage in ("migration-check", "migrate"):
            require(HASH.fullmatch(op.get("migration_set_sha256", "")), "migration set checksum required")


def validate(release, targets, ready=False, scope="release"):
    require(release.get("schema_version") == 1, "unsupported release schema")
    require(targets.get("schema_version") == 1, "unsupported target schema")
    require(release.get("state") in ("draft", "ready"), "state must be draft or ready")
    require(NAME.fullmatch(release.get("release_id", "")), "invalid release_id")
    require(targets.get("environment") == "production", "only production targets are supported")
    require(set(release.get("stages", {})) == set(ALL_STAGES), "all seven stages must be explicit")
    require(isinstance(release.get("components"), dict) and release["components"], "components required")
    missing = []
    for field, pattern in (("account_id", r"\d{12}"), ("region", r"[a-z]{2}-[a-z]+-\d"),
                           ("receipt_bucket", r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")):
        value = targets.get(field)
        if value is None:
            missing.append(f"targets.{field}")
        else:
            require(re.fullmatch(pattern, value), f"invalid targets.{field}")
    if targets.get("runner_image") is None:
        missing.append("targets.runner_image")
    else:
        require(re.fullmatch(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+@sha256:[0-9a-f]{64}", targets["runner_image"]),
                "CodeBuild runner image must be pinned by ECR digest")
    if targets.get("receipt_kms_key_arn") is None:
        missing.append("targets.receipt_kms_key_arn")
    else:
        require(re.fullmatch(rf"arn:aws:kms:{targets['region']}:{targets['account_id']}:key/[a-f0-9-]+", targets["receipt_kms_key_arn"]),
                "production receipt KMS key ARN required")
    projects = targets.get("projects", {})
    require(isinstance(projects, dict), "projects must be an object")
    for name, c in release["components"].items():
        require(NAME.fullmatch(name), "invalid component name")
        require(REPO.fullmatch(c.get("repository", "")), f"invalid repository: {name}")
        for field in ("source_sha", "ci_run_id", "ci_workflow"):
            if c.get(field) is None:
                missing.append(f"components.{name}.{field}")
        if c.get("source_sha") is not None:
            require(SHA.fullmatch(c["source_sha"]), f"full source SHA required: {name}")
        if c.get("ci_run_id") is not None:
            require(type(c["ci_run_id"]) is int and c["ci_run_id"] > 0, "invalid CI run ID")
        if c.get("ci_workflow") is not None:
            require(re.fullmatch(r"\.github/workflows/[a-zA-Z0-9_.-]+\.ya?ml", c["ci_workflow"]),
                    "CI workflow must be an explicit workflow file")
        artifacts = c.get("artifacts")
        if artifacts is None:
            missing.append(f"components.{name}.artifacts")
        else:
            require(isinstance(artifacts, list), "artifacts must be a list")
            for artifact in artifacts:
                require(artifact.get("kind") in ("ecr", "s3"), "artifact kind must be ecr or s3")
                if artifact["kind"] == "ecr":
                    require(re.fullmatch(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+@sha256:[0-9a-f]{64}",
                                         artifact.get("uri", "")), "ECR artifacts require a digest URI")
                else:
                    require(artifact.get("bucket") and artifact.get("key") and
                            artifact.get("version_id") not in (None, "", "null") and
                            HASH.fullmatch(artifact.get("sha256", "")), "S3 artifact must pin version and checksum")
    for stage, entries in release["stages"].items():
        require(isinstance(entries, list) and entries, f"stage cannot be empty: {stage}")
        require(len(set(entries)) == len(entries), f"duplicate stage entry: {stage}")
        for entry in entries:
            require(entry in projects, f"unknown project binding: {entry}")
            project = projects[entry]
            require(project.get("stage") == stage, f"project bound to wrong stage: {entry}")
            require(project.get("component") in release["components"], f"unknown component: {entry}")
            require(re.fullmatch(r"buildspec(?:[a-z0-9_./-]*)\.ya?ml|\.buildspecs/[a-z0-9_./-]+\.ya?ml", project.get("buildspec", ""))
                    and ".." not in project["buildspec"], f"invalid buildspec path: {entry}")
            operation = release.get("operations", {}).get(entry)
            if operation is None:
                missing.append(f"operations.{entry}")
            else:
                validate_operation(stage, operation, targets)
            if project.get("name") is None:
                missing.append(f"projects.{entry}.name")
            else:
                require(re.fullmatch(r"velnor-prod-[a-z0-9-]+", project["name"]), "production project name required")
    infra_plan = release.get("operations", {}).get("infra-plan")
    infra_apply = release.get("operations", {}).get("infra-apply")
    require(infra_plan == infra_apply, "infrastructure plan/apply must use identical workspace inputs")
    check_ops = [release.get("operations", {}).get(n) for n in release["stages"]["migration-check"]]
    apply_ops = [release.get("operations", {}).get(n) for n in release["stages"]["migrate"]]
    if all(check_ops + apply_ops):
        require({x["migration_set_sha256"] for x in check_ops} == {x["migration_set_sha256"] for x in apply_ops},
                "migration check and apply must bind the same migration set")
    if ready:
        # Infrastructure can be prepared before application commits/task revisions exist.
        if scope in ("infra-plan", "infra-apply"):
            selected = set(release["stages"]["infra-plan"] + release["stages"]["infra-apply"])
            components = {projects[x]["component"] for x in selected}
            missing = [m for m in missing if m.startswith("targets.") or
                       (m.startswith("components.") and m.split(".")[1] in components) or
                       (m.startswith("projects.") and m.split(".")[1] in selected) or
                       (m.startswith("operations.") and m.split(".")[1] in selected)]
        require(release["state"] == "ready", "release remains draft; pin it after development settles")
        require(not missing, "unresolved release bindings: " + ", ".join(missing))
    return missing


def aws(api, operation, **payload):
    from oidc import refresh
    refresh()
    command = ["aws", api, operation, "--region", os.environ["AWS_REGION"],
               "--no-cli-pager", "--output", "json"]
    if payload:
        command += ["--cli-input-json", json.dumps(payload)]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def github(path):
    token = os.environ.get("PRODUCTION_SOURCE_READ_TOKEN", "")
    require(token, "PRODUCTION_SOURCE_READ_TOKEN is required to verify private source CI")
    request = Request("https://api.github.com" + path, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_ci(release, components=None):
    for name, component in release["components"].items():
        if components is not None and name not in components:
            continue
        repo = component["repository"]
        run = github(f"/repos/{repo}/actions/runs/{component['ci_run_id']}")
        require(run.get("repository", {}).get("full_name") == repo, f"wrong CI repository: {name}")
        require(run.get("head_sha") == component["source_sha"], f"CI SHA mismatch: {name}")
        require(run.get("status") == "completed" and run.get("conclusion") == "success", f"CI not successful: {name}")
        require(run.get("event") in ("push", "workflow_dispatch") and run.get("head_branch") == "main",
                f"CI must validate main, not a pull-request merge: {name}")
        require(run.get("path") == component["ci_workflow"], f"wrong CI workflow: {name}")


def verify_identity(targets):
    require(aws("sts", "get-caller-identity")["Account"] == targets["account_id"], "AWS account mismatch")
    require(os.environ["AWS_REGION"] == targets["region"], "AWS region mismatch")
    require(aws("s3api", "get-bucket-versioning", Bucket=targets["receipt_bucket"]).get("Status") == "Enabled",
            "receipt bucket must have versioning enabled")


def verify_project(binding, component, targets):
    result = aws("codebuild", "batch-get-projects", names=[binding["name"]])
    require(not result.get("projectsNotFound") and len(result.get("projects", [])) == 1, "production project missing")
    project = result["projects"][0]
    arn_prefix = f"arn:aws:codebuild:{targets['region']}:{targets['account_id']}:project/"
    require(project["arn"] == arn_prefix + binding["name"], "CodeBuild account/region mismatch")
    tags = {t["key"]: t["value"] for t in project.get("tags", [])}
    require(tags.get("Environment") in ("prod", "production"), "CodeBuild production tag required")
    source = project.get("source", {})
    require(source.get("type") == "GITHUB" and source.get("location", "").removesuffix(".git") ==
            "https://github.com/" + component["repository"], "project source repository mismatch")
    require(source.get("buildspec") == binding["buildspec"], "project buildspec mismatch")
    secondary = project.get("secondarySources", [])
    require(len(secondary) == 1 and secondary[0].get("sourceIdentifier") == "controller" and
            secondary[0].get("type") == "GITHUB" and secondary[0].get("location", "").removesuffix(".git") ==
            "https://github.com/VelnorLabs/.github", "exactly one controller secondary source is required")
    require(project.get("environment", {}).get("image") == targets["runner_image"], "unreviewed runner image")
    require(project.get("serviceRole", "").startswith(f"arn:aws:iam::{targets['account_id']}:role/velnor-prod-"),
            "project requires a production-scoped service role")
    artifact = project.get("artifacts", {})
    require(artifact.get("type") == "S3" and artifact.get("location") == targets["receipt_bucket"] and
            artifact.get("packaging", "NONE") == "NONE" and not artifact.get("encryptionDisabled", False),
            "project must emit unzipped encrypted receipts into the production receipt bucket")


def ensure_idle(targets):
    """A cancelled GitHub run can leave a remote build alive; don't overlap it."""
    for binding in targets["projects"].values():
        if binding["name"] is None:
            continue
        next_token = None
        while True:
            request = {"projectName": binding["name"], "sortOrder": "DESCENDING"}
            if next_token:
                request["nextToken"] = next_token
            result = aws("codebuild", "list-builds-for-project", **request)
            ids = result.get("ids", [])
            if ids:
                builds = aws("codebuild", "batch-get-builds", ids=ids).get("builds", [])
                require(not any(b.get("buildStatus") == "IN_PROGRESS" for b in builds),
                        f"production build still running in {binding['name']}; inspect/stop it before retrying")
            next_token = result.get("nextToken")
            if not next_token:
                break


def load_receipt(build, expected, targets):
    location = build.get("artifacts", {}).get("location", "")
    prefix = f"arn:aws:s3:::{targets['receipt_bucket']}/"
    require(location.startswith(prefix), "unexpected receipt artifact destination")
    key = location[len(prefix):].rstrip("/") + "/production-receipt.json"
    head = aws("s3api", "head-object", Bucket=targets["receipt_bucket"], Key=key)
    version = head.get("VersionId")
    require(version not in (None, "", "null"), "receipt must have an immutable S3 version")
    require(head.get("ServerSideEncryption") == "aws:kms" and head.get("SSEKMSKeyId") == targets["receipt_kms_key_arn"], "receipt requires the configured KMS key")
    require(head.get("ContentLength", 0) <= 1024 * 1024, "receipt exceeds 1 MiB")
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "receipt.json"
        subprocess.run(["aws", "s3api", "get-object", "--bucket", targets["receipt_bucket"],
                        "--key", key, "--version-id", version, "--region", targets["region"],
                        str(output)], check=True, capture_output=True, timeout=120)
        receipt = read_json(output)
    for field, value in expected.items():
        require(receipt.get(field) == value, f"receipt mismatch: {field}")
    require(receipt.get("status") == "success", "stage did not attest success")
    require(isinstance(receipt.get("evidence"), dict) and receipt["evidence"], "stage evidence is missing")
    return receipt, {"bucket": targets["receipt_bucket"], "key": key, "version_id": version}


def publish_release(release, targets):
    import tempfile
    key = "production/manifests/" + digest(release) + ".json"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "release.json"
        path.write_bytes(canonical(release))
        result = subprocess.run(["aws", "s3api", "put-object", "--bucket", targets["receipt_bucket"],
            "--key", key, "--body", str(path), "--server-side-encryption", "aws:kms",
            "--ssekms-key-id", targets["receipt_kms_key_arn"], "--region", targets["region"], "--output", "json"],
            check=True, capture_output=True, text=True, timeout=120)
    version = json.loads(result.stdout).get("VersionId")
    require(version not in (None, "", "null"), "manifest upload must return an immutable S3 version")
    return {"VELNOR_RELEASE_KEY": key, "VELNOR_RELEASE_VERSION": version}


def execute_stage(stage, release, targets, context, plan_sha="", timeout=3300):
    results = []
    release_address = publish_release(release, targets)
    for name in release["stages"][stage]:
        binding = targets["projects"][name]
        component = release["components"][binding["component"]]
        verify_project(binding, component, targets)
        token = digest({"run": context, "stage": stage, "project": name})
        # SOURCE_VERSION is set by AWS, never a branch supplied by a project.
        values = {**release_address, "VELNOR_RELEASE_SHA256": digest(release), "VELNOR_TARGETS_SHA256": digest(targets),
                  "VELNOR_STAGE": stage, "VELNOR_PROJECT_BINDING": name,
                  "VELNOR_CONTROLLER_RUN": context, "VELNOR_EXPECTED_PLAN_SHA256": plan_sha,
                  "VELNOR_CONTROLLER_SHA": os.environ["GITHUB_SHA"],
                  "VELNOR_PRODUCTION_ACCOUNT": targets["account_id"], "VELNOR_RECEIPT_BUCKET": targets["receipt_bucket"],
                  "VELNOR_RECEIPT_KMS_KEY": targets["receipt_kms_key_arn"]}
        result = aws("codebuild", "start-build", projectName=binding["name"],
                     sourceVersion=component["source_sha"], idempotencyToken=token,
                     autoRetryLimitOverride=0, encryptionKeyOverride=targets["receipt_kms_key_arn"],
                     secondarySourcesVersionOverride=[{"sourceIdentifier": "controller", "sourceVersion": os.environ["GITHUB_SHA"]}],
                     buildspecOverride=(ROOT / "production/buildspecs" / (stage + ".yml")).read_text(),
                     artifactsOverride={"type": "S3", "location": targets["receipt_bucket"],
                                        "path": "production/" + context, "namespaceType": "BUILD_ID",
                                        "name": name, "packaging": "NONE", "overrideArtifactName": False,
                                        "encryptionDisabled": False},
                     environmentVariablesOverride=[{"name": k, "value": v, "type": "PLAINTEXT"}
                                                   for k, v in values.items()],
                     timeoutInMinutesOverride=50, queuedTimeoutInMinutesOverride=5)
        build_id = result["build"]["id"]
        print(f"{stage}: {name}: {build_id}", flush=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                builds = aws("codebuild", "batch-get-builds", ids=[build_id]).get("builds", [])
                require(len(builds) == 1, "build disappeared while waiting")
                build = builds[0]
                status = build["buildStatus"]
                if status != "IN_PROGRESS":
                    require(status == "SUCCEEDED", f"{stage}/{name} failed: {status}; inspect {build_id}")
                    break
                require(time.monotonic() < deadline, f"{stage}/{name} timed out")
                time.sleep(15)
        except (GateError, KeyboardInterrupt, subprocess.SubprocessError):
            aws("codebuild", "stop-build", id=build_id)
            raise
        require(build.get("resolvedSourceVersion") == component["source_sha"], "CodeBuild resolved wrong source SHA")
        receipt, address = load_receipt(build, {"schema_version": 1, "stage": stage,
            "build_id": build_id, "source_sha": component["source_sha"],
            "release_sha256": digest(release), "targets_sha256": digest(targets),
            "controller_run": context, "project_binding": name, "controller_sha": os.environ["GITHUB_SHA"]}, targets)
        if stage == "infra-plan":
            require(HASH.fullmatch(receipt.get("plan_sha256", "")), "plan receipt must pin saved plan checksum")
        if stage == "infra-apply":
            require(receipt.get("applied_plan_sha256") == plan_sha, "applied plan does not match reviewed plan")
        if stage == "migration-check":
            require(receipt.get("destructive_changes") is False, "destructive migrations require a separate reviewed procedure")
        results.append({"project": name, "build_id": build_id, "receipt": address, "result": receipt})
    return results


def preview(release, targets):
    missing = validate(release, targets)
    lines = [f"# Production release: {release['release_id']}", "",
             f"State: **{release['state']}**", f"Release SHA256: `{digest(release)}`",
             f"Targets SHA256: `{digest(targets)}`", "", "## Execution order"]
    for stage in ALL_STAGES:
        lines.append(f"- {stage}: " + ", ".join(release["stages"][stage]))
    lines += ["", "## Pins"]
    for name, c in release["components"].items():
        lines.append(f"- {name}: {c['repository']} @ {c.get('source_sha') or 'UNPINNED'}")
    lines += ["", "## Unresolved bindings", *[f"- {x}" for x in missing]]
    if not missing:
        lines.append("- None. Live CI, account, project and receipt checks still run before promotion.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preview", "check-ci", "release", "infra-plan", "infra-apply"))
    parser.add_argument("--release", default="production/releases/candidate.json")
    parser.add_argument("--targets", default="production/targets/production.json")
    parser.add_argument("--plan-sha256", default="")
    args = parser.parse_args()
    release = read_json(checked_file(args.release, "production/releases"))
    targets = read_json(checked_file(args.targets, "production/targets"))
    validate(release, targets, ready=args.operation != "preview", scope=args.operation)
    summary = preview(release, targets)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
    if args.operation == "preview":
        return
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "production controller must run from main")
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "production requires manual dispatch")
    require(os.environ.get("PRODUCTION_PIPELINES_ENABLED") == "true", "production pipelines are not activated")
    selected_components = None
    if args.operation in ("infra-plan", "infra-apply"):
        selected_components = {targets["projects"][x]["component"] for x in release["stages"][args.operation]}
    verify_ci(release, selected_components)
    if args.operation == "check-ci":
        return
    verify_identity(targets)
    require(args.operation != "infra-apply" or HASH.fullmatch(args.plan_sha256),
            "infra-apply requires the reviewed saved plan SHA256")
    context = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    require(re.fullmatch(r"\d+-\d+", context), "invalid controller run identity")
    stages = STAGES if args.operation == "release" else (args.operation,)
    # Preflight EVERY project before the first migration or other mutation.
    for stage in stages:
        for name in release["stages"][stage]:
            b = targets["projects"][name]
            verify_project(b, release["components"][b["component"]], targets)
    ensure_idle(targets)
    results = []
    try:
        for stage in stages:
            results.extend(execute_stage(stage, release, targets, context, args.plan_sha256))
    finally:
        # CodeBuild owns durable failure logs; this summary contains only evidence addresses.
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write("\n## Completed stages\n```json\n" + json.dumps(results, indent=2) + "\n```\n")


if __name__ == "__main__":
    try:
        main()
    except (GateError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Production gate failed: {exc}", file=sys.stderr)
        sys.exit(1)
