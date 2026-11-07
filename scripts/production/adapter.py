#!/usr/bin/env python3
"""CodeBuild production adapters. Invoked only by the pinned controller buildspec.

Resource IDs and artifact pins are supplied later in release.operations. No
defaults point at development. IAM in each project remains the resource boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True, timeout=1800).stdout


def aws(api, operation, **payload):
    value = run("aws", api, operation, "--cli-input-json", json.dumps(payload), "--output", "json", "--no-cli-pager")
    return json.loads(value) if value.strip() else {}


def sha(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def arn(value, service, kind):
    check(value.startswith(f"arn:aws:{service}:{os.environ['AWS_REGION']}:{os.environ['VELNOR_PRODUCTION_ACCOUNT']}:{kind}"),
          "resource ARN must belong to the configured production account/region")


def download(bucket, key, path, version=None):
    args = ["aws", "s3api", "get-object", "--bucket", bucket, "--key", key]
    if version:
        args += ["--version-id", version]
    run(*args, str(path))


def verify_images(task, images):
    actual = {c["name"]: c["image"] for c in task["containerDefinitions"]}
    check(actual == images and images, "all task containers (including sidecars) must match the manifest")
    for image in images.values():
        check(re.fullmatch(r"\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+@sha256:[0-9a-f]{64}", image),
              "every container must use a private ECR digest URI")


def protect_images(images):
    """Retain promoted image manifests under the existing deployed-* convention."""
    for image in set(images.values()):
        registry, rest = image.split("/", 1)
        repository, image_digest = rest.split("@", 1)
        account = registry.split(".")[0]
        check(account == os.environ["VELNOR_PRODUCTION_ACCOUNT"], "image must be available in production ECR")
        tag = "deployed-" + image_digest.split(":")[1]
        existing = aws("ecr", "batch-get-image", registryId=account, repositoryName=repository, imageIds=[{"imageTag": tag}])
        if existing.get("images"):
            check(existing["images"][0]["imageId"]["imageDigest"] == image_digest, "retention tag digest mismatch")
            continue
        check(all(f.get("failureCode") == "ImageNotFound" for f in existing.get("failures", [])), "ECR retention lookup failed")
        found = aws("ecr", "batch-get-image", registryId=account, repositoryName=repository, imageIds=[{"imageDigest": image_digest}])
        check(not found.get("failures") and len(found.get("images", [])) == 1, "pinned image missing from production ECR")
        manifest = found["images"][0]
        aws("ecr", "put-image", registryId=account, repositoryName=repository, imageManifest=manifest["imageManifest"],
            imageManifestMediaType=manifest["imageManifestMediaType"], imageTag=tag, imageDigest=image_digest)


def backend(op):
    # Task definitions are registered by the reviewed infrastructure release;
    # the application pipeline updates the service to an explicit revision.
    if op["kind"] == "ecs-service":
        arn(op["cluster"], "ecs", "cluster/")
        arn(op["task_definition"], "ecs", "task-definition/")
        check(re.search(r":\d+$", op["task_definition"]), "task definition revision required")
        task = aws("ecs", "describe-task-definition", taskDefinition=op["task_definition"])["taskDefinition"]
        verify_images(task, op["images"])
        protect_images(op["images"])
        before = aws("ecs", "describe-services", cluster=op["cluster"], services=[op["service"]])
        check(not before.get("failures") and len(before.get("services", [])) == 1, "service missing")
        service = before["services"][0]
        check(service.get("desiredCount", 0) > 0 and service.get("deploymentController", {}).get("type", "ECS") == "ECS",
              "only active ECS rolling services are supported")
        previous = service["taskDefinition"]
        # Persist the rollback pointer before mutation, even if stabilization fails.
        record({"kind": "ecs-service", "service": op["service"], "previous_task_definition": previous,
                "target_task_definition": op["task_definition"]}, "rollback.json")
        aws("ecs", "update-service", cluster=op["cluster"], service=op["service"], taskDefinition=op["task_definition"])
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            value = aws("ecs", "describe-services", cluster=op["cluster"], services=[op["service"]])
            check(not value.get("failures"), "service lookup failed")
            state = value["services"][0]
            primary = [d for d in state["deployments"] if d["status"] == "PRIMARY"]
            check(len(primary) == 1 and primary[0]["taskDefinition"] == op["task_definition"], "ECS rolled back or another deployment replaced this release")
            check(primary[0].get("rolloutState") != "FAILED", "ECS rollout failed")
            if (len(state["deployments"]) == 1 and primary[0].get("rolloutState") == "COMPLETED"
                    and state["pendingCount"] == 0 and state["runningCount"] == state["desiredCount"]):
                return {"service": op["service"], "task_definition": op["task_definition"], "previous": previous,
                        "running": state["runningCount"], "images": op["images"]}
            time.sleep(15)
        raise RuntimeError("ECS deployment timed out; inspect rollback.json; no automatic data rollback")
    if op["kind"] == "lambda-alias":
        arn(op["function"], "lambda", "function:")
        check(re.fullmatch(r"[1-9][0-9]*", op["version"]), "published Lambda version required")
        check(op["alias"] == "production", "only the production alias can be moved")
        target = aws("lambda", "get-function", FunctionName=op["function"], Qualifier=op["version"])
        check(target["Configuration"]["CodeSha256"] == op["code_sha256"], "Lambda code checksum mismatch")
        before = aws("lambda", "get-alias", FunctionName=op["function"], Name=op["alias"])
        record({"function": op["function"], "alias": op["alias"], "previous": before}, "rollback.json")
        updated = aws("lambda", "update-alias", FunctionName=op["function"], Name=op["alias"],
                      FunctionVersion=op["version"], RevisionId=before["RevisionId"], RoutingConfig={"AdditionalVersionWeights": {}})
        check(updated["FunctionVersion"] == op["version"], "Lambda alias did not move")
        return {"function": op["function"], "version": op["version"], "previous_version": before["FunctionVersion"]}
    raise RuntimeError("unsupported backend kind; Step Functions and other resources belong to reviewed IaC")


def task_stage(op, stage):
    arn(op["cluster"], "ecs", "cluster/")
    arn(op["task_definition"], "ecs", "task-definition/")
    check(re.search(r":\d+$", op["task_definition"]), "task revision required")
    definition = aws("ecs", "describe-task-definition", taskDefinition=op["task_definition"])["taskDefinition"]
    verify_images(definition, op["images"])
    protect_images(op["images"])
    name = op["container"]
    check(name in op["images"], "task container missing")
    prefix = "task-evidence/" + os.environ["CODEBUILD_BUILD_ID"].replace(":", "/")
    env = {"VELNOR_RELEASE_SHA256": os.environ["VELNOR_RELEASE_SHA256"], "VELNOR_STAGE": stage,
           "VELNOR_EVIDENCE_BUCKET": os.environ["VELNOR_RECEIPT_BUCKET"], "VELNOR_EVIDENCE_KEY": prefix + ".json",
           "VELNOR_MIGRATION_SET_SHA256": op.get("migration_set_sha256", "")}
    # Task entrypoint/command are part of the pinned task definition, not shell input.
    result = aws("ecs", "run-task", cluster=op["cluster"], taskDefinition=op["task_definition"],
                 launchType="FARGATE", count=1, clientToken=hashlib.sha256(prefix.encode()).hexdigest(),
                 networkConfiguration={"awsvpcConfiguration": {"subnets": op["subnets"],
                     "securityGroups": op["security_groups"], "assignPublicIp": "DISABLED"}},
                 overrides={"containerOverrides": [{"name": name, "environment": [{"name": k, "value": v} for k, v in env.items()]}]})
    check(not result.get("failures") and len(result.get("tasks", [])) == 1, "Fargate task placement failed")
    task_arn = result["tasks"][0]["taskArn"]
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline:
        tasks = aws("ecs", "describe-tasks", cluster=op["cluster"], tasks=[task_arn])
        check(not tasks.get("failures") and len(tasks.get("tasks", [])) == 1, "task lookup failed")
        task = tasks["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            required = {c["name"] for c in definition["containerDefinitions"] if c.get("essential", True)}
            codes = {c["name"]: c.get("exitCode") for c in task["containers"]}
            check(all(codes.get(n) == 0 for n in required | {name}), "task exited unsuccessfully")
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "evidence.json"
                head = aws("s3api", "head-object", Bucket=env["VELNOR_EVIDENCE_BUCKET"], Key=env["VELNOR_EVIDENCE_KEY"])
                check(head.get("VersionId") not in (None, "", "null") and head.get("ServerSideEncryption") == "aws:kms",
                      "task evidence must be versioned and encrypted")
                download(env["VELNOR_EVIDENCE_BUCKET"], env["VELNOR_EVIDENCE_KEY"], p, head["VersionId"])
                evidence = json.loads(p.read_text())
            check(evidence.get("release_sha256") == env["VELNOR_RELEASE_SHA256"] and evidence.get("stage") == stage
                  and evidence.get("status") == "success", "task evidence is not bound to this release/stage")
            if stage in ("migration-check", "migrate"):
                check(evidence.get("migration_set_sha256") == op["migration_set_sha256"], "migration set changed")
            if stage == "migration-check":
                check(evidence.get("destructive_changes") is False, "destructive migrations are not allowed")
            return {"task": task_arn, "verified": evidence}
        time.sleep(15)
    aws("ecs", "stop-task", cluster=op["cluster"], task=task_arn, reason="Production controller timeout")
    raise RuntimeError("task stage timed out")


def record(value, filename):
    Path(filename).write_text(json.dumps(value, indent=2) + "\n")
    key = "adapter-records/" + os.environ["CODEBUILD_BUILD_ID"].replace(":", "/") + "/" + filename
    run("aws", "s3api", "put-object", "--bucket", os.environ["VELNOR_RECEIPT_BUCKET"], "--key", key,
        "--body", filename, "--server-side-encryption", "aws:kms", "--ssekms-key-id", os.environ["VELNOR_RECEIPT_KMS_KEY"])


def web(op):
    # Promote a production-configured, tested bundle; never publish a development build.
    check(op["environment"] == "production", "web artifact must be production-configured")
    bucket = op["destination_bucket"]
    check(bucket.startswith("velnor-prod-"), "production web bucket required")
    check(aws("s3api", "get-bucket-versioning", Bucket=bucket).get("Status") == "Enabled", "Web bucket versioning required")
    distribution = aws("cloudfront", "get-distribution", Id=op["distribution_id"])["Distribution"]
    check(distribution["ARN"].split(":")[4] == os.environ["VELNOR_PRODUCTION_ACCOUNT"], "CloudFront account mismatch")
    origins = [o["DomainName"] for o in distribution["DistributionConfig"]["Origins"]["Items"]]
    check(any(o.startswith(bucket + ".s3") for o in origins), "distribution does not serve the selected bucket")
    with tempfile.TemporaryDirectory() as d:
        bundle = Path(d) / "bundle.tar.gz"
        download(op["artifact"]["bucket"], op["artifact"]["key"], bundle, op["artifact"]["version_id"])
        check(sha(bundle) == op["artifact"]["sha256"], "web bundle checksum mismatch")
        dest = Path(d) / "site"
        dest.mkdir()
        with tarfile.open(bundle) as archive:
            for member in archive.getmembers():
                check(not member.issym() and not member.islnk() and not member.isdev(), "unsafe web archive member")
                check((dest / member.name).resolve().is_relative_to(dest), "archive path traversal")
            archive.extractall(dest, filter="data")
        check((dest / "index.html").is_file(), "bundle root must contain index.html")
        manifest = json.loads((dest / "production-build.json").read_text())
        check(manifest == op["build_metadata"], "production bundle metadata mismatch")
        check(manifest["environment"] == "production" and manifest["source_sha"] == os.environ["CODEBUILD_RESOLVED_SOURCE_VERSION"],
              "production bundle is not from the pinned source")
        # Version listing works for a first deployment too; don't swallow AccessDenied as 'missing'.
        previous = aws("s3api", "list-object-versions", Bucket=bucket, Prefix="index.html")
        record({"bucket": bucket, "index_versions": previous.get("Versions", [])}, "rollback.json")
        run("aws", "s3", "sync", str(dest), "s3://" + bucket + "/", "--exclude", "*.html",
            "--cache-control", "no-cache", "--sse", "aws:kms", "--sse-kms-key-id", op["destination_kms_key_arn"])
        # No --delete: old hashed assets remain available to installed/active clients.
        for page in sorted(dest.rglob("*.html"), key=lambda p: p.name == "index.html"):
            run("aws", "s3", "cp", str(page), "s3://" + bucket + "/" + page.relative_to(dest).as_posix(),
                "--cache-control", "no-cache", "--content-type", "text/html", "--sse", "aws:kms", "--sse-kms-key-id", op["destination_kms_key_arn"])
        invalidation = aws("cloudfront", "create-invalidation", DistributionId=op["distribution_id"],
                           InvalidationBatch={"Paths": {"Quantity": 1, "Items": ["/*"]},
                                              "CallerReference": os.environ["CODEBUILD_BUILD_ID"]})["Invalidation"]["Id"]
        run("aws", "cloudfront", "wait", "invalidation-completed", "--distribution-id", op["distribution_id"], "--id", invalidation)
        return {"bucket": bucket, "artifact_sha256": op["artifact"]["sha256"], "invalidation": invalidation}


def check_plan(plan):
    """Infrastructure preparation cannot secretly promote existing applications."""
    for change in plan.get("resource_changes", []):
        actions = change["change"]["actions"]
        check("delete" not in actions, "destructive infrastructure changes need a separate reviewed procedure")
        before, after = change["change"].get("before"), change["change"].get("after")
        if not before or not after:
            continue
        fields = {"aws_ecs_service": ("task_definition",), "aws_lambda_alias": ("function_version",)}.get(change["type"], ())
        for field in fields:
            unknown = change["change"].get("after_unknown", {}).get(field)
            check(not unknown and before.get(field) == after.get(field),
                  "infrastructure cannot promote an existing application before the migration gate")


def infrastructure(op, stage):
    check(json.loads(run("tofu", "version", "-json"))["terraform_version"] == op["tofu_version"],
          "CodeBuild image has the wrong OpenTofu version")
    workspaces = op["workspaces"]
    source = Path(os.environ["CODEBUILD_SRC_DIR"]).resolve()
    for item in workspaces:
        p = (source / item["path"]).resolve()
        check(p.is_relative_to(source / "accounts/prod") and p.is_dir(), "workspace must exist inside accounts/prod")
        check((p / ".terraform.lock.hcl").is_file(), "committed provider lock required")
        run("tofu", "init", "-input=false", "-lockfile=readonly", cwd=p)
    bucket = os.environ["VELNOR_RECEIPT_BUCKET"]
    expected_metadata = {"release_sha256": os.environ["VELNOR_RELEASE_SHA256"],
                         "source_sha": os.environ["CODEBUILD_RESOLVED_SOURCE_VERSION"],
                         "targets_sha256": os.environ["VELNOR_TARGETS_SHA256"],
                         "controller_sha": os.environ["VELNOR_CONTROLLER_SHA"], "workspaces": workspaces}
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "plans"
        folder.mkdir()
        bundle = Path(d) / "plans.tar.gz"
        if stage == "infra-plan":
            for i, item in enumerate(workspaces):
                p = source / item["path"]
                variables = folder / f"{i}.tfvars.json"
                variables.write_text(json.dumps(item["variables"]))
                run("tofu", "plan", "-input=false", "-lock-timeout=5m", "-var-file=" + str(variables),
                    "-out=" + str(folder / f"{i}.tfplan"), cwd=p)
                check_plan(json.loads(run("tofu", "show", "-json", str(folder / f"{i}.tfplan"), cwd=p)))
                (folder / f"{i}.review.txt").write_text(run("tofu", "show", "-no-color", str(folder / f"{i}.tfplan"), cwd=p))
            (folder / "metadata.json").write_text(json.dumps(expected_metadata))
            with tarfile.open(bundle, "w:gz") as archive:
                for p in sorted(folder.iterdir()):
                    archive.add(p, arcname=p.name)
            checksum = sha(bundle)
            run("aws", "s3api", "put-object", "--bucket", bucket, "--key", "production/plans/" + checksum + ".tar.gz",
                "--body", str(bundle), "--server-side-encryption", "aws:kms", "--ssekms-key-id", os.environ["VELNOR_RECEIPT_KMS_KEY"])
            return {"plan_sha256": checksum, "plan_bucket": bucket, "plan_key": "production/plans/" + checksum + ".tar.gz"}
        checksum = os.environ["VELNOR_EXPECTED_PLAN_SHA256"]
        check(re.fullmatch(r"[0-9a-f]{64}", checksum), "reviewed plan hash required")
        download(bucket, "production/plans/" + checksum + ".tar.gz", bundle)
        check(sha(bundle) == checksum, "saved plan bundle checksum mismatch")
        with tarfile.open(bundle) as archive:
            for member in archive.getmembers():
                check(member.isfile() and "/" not in member.name and member.name not in (".", ".."), "unsafe plan bundle")
            archive.extractall(folder, filter="data")
        check(json.loads((folder / "metadata.json").read_text()) == expected_metadata, "plan belongs to another source, target or release")
        for i, item in enumerate(workspaces):
            run("tofu", "apply", "-input=false", "-lock-timeout=5m", str(folder / f"{i}.tfplan"), cwd=source / item["path"])
        return {"applied_plan_sha256": checksum, "workspaces": [x["path"] for x in workspaces]}


def main():
    stage = os.environ["VELNOR_STAGE"]
    with tempfile.TemporaryDirectory() as d:
        release_path = Path(d) / "release.json"
        download(os.environ["VELNOR_RECEIPT_BUCKET"], os.environ["VELNOR_RELEASE_KEY"], release_path,
                 os.environ["VELNOR_RELEASE_VERSION"])
        release = json.loads(release_path.read_text())
    check(hashlib.sha256(json.dumps(release, sort_keys=True, separators=(",", ":")).encode()).hexdigest() ==
          os.environ["VELNOR_RELEASE_SHA256"], "release payload checksum mismatch")
    controller = Path(os.environ["CODEBUILD_SRC_DIR_controller"])
    check(run("git", "rev-parse", "HEAD", cwd=controller).strip() == os.environ["VELNOR_CONTROLLER_SHA"], "controller source mismatch")
    check(aws("sts", "get-caller-identity")["Account"] == os.environ["VELNOR_PRODUCTION_ACCOUNT"], "wrong AWS account")
    binding = os.environ["VELNOR_PROJECT_BINDING"]
    op = release["operations"][binding]
    if stage in ("infra-plan", "infra-apply"):
        evidence = infrastructure(op, stage)
    elif stage == "backend":
        evidence = backend(op)
    elif stage == "web":
        evidence = web(op)
    else:
        evidence = task_stage(op, stage)
    Path("production-evidence.json").write_text(json.dumps(evidence))
    command = ["python3", str(controller / "scripts/production/receipt.py"), "--evidence", "production-evidence.json"]
    if stage == "infra-plan":
        command += ["--plan-sha256", evidence["plan_sha256"]]
    elif stage == "infra-apply":
        command += ["--applied-plan-sha256", evidence["applied_plan_sha256"]]
    elif stage == "migration-check":
        command += ["--no-destructive-changes"]
    run(*command)


if __name__ == "__main__":
    main()
