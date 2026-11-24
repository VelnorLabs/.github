#!/usr/bin/env python3
"""Emit a receipt AFTER a production adapter has performed and verified its work.

Copy this helper into the source repo's buildspec support directory, pinning the
controller revision in that repo. It cannot make a failed build successful.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, help="JSON object produced by actual verification")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--applied-plan-sha256")
    parser.add_argument("--no-destructive-changes", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CODEBUILD_BUILD_SUCCEEDING") != "1":
        raise SystemExit("Refusing to emit success for a failed build")
    evidence = json.loads(Path(args.evidence).read_text())
    if not isinstance(evidence, dict) or not evidence:
        raise SystemExit("Verification evidence must be a nonempty object")
    receipt = {"schema_version": 1, "status": "success", "evidence": evidence}
    for field, env in {"stage": "VELNOR_STAGE", "build_id": "CODEBUILD_BUILD_ID",
                       "source_sha": "CODEBUILD_RESOLVED_SOURCE_VERSION",
                       "release_sha256": "VELNOR_RELEASE_SHA256", "targets_sha256": "VELNOR_TARGETS_SHA256",
                       "controller_run": "VELNOR_CONTROLLER_RUN", "controller_sha": "VELNOR_CONTROLLER_SHA", "project_binding": "VELNOR_PROJECT_BINDING"}.items():
        receipt[field] = os.environ[env]
    if args.plan_sha256:
        receipt["plan_sha256"] = args.plan_sha256
    if args.applied_plan_sha256:
        receipt["applied_plan_sha256"] = args.applied_plan_sha256
    if args.no_destructive_changes:
        receipt["destructive_changes"] = False
    Path("production-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
