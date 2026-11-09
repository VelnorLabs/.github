#!/usr/bin/env python3
"""Assume only the configured production role; compatible with local_only actions policy."""
import json
import os
import re
import subprocess
import time
from urllib.request import Request, urlopen

_expires = 0


def refresh():
    global _expires
    if time.time() < _expires:
        return
    account = os.environ.get("PRODUCTION_ACCOUNT_ID", "")
    role = os.environ.get("PRODUCTION_ROLE_ARN", "")
    if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(
        rf"arn:aws:iam::{account}:role/velnor-prod-[A-Za-z0-9+=,.@_-]+", role
    ):
        raise RuntimeError("Explicit production account and production-scoped role are required")
    if os.environ.get("PRODUCTION_PIPELINES_ENABLED") != "true":
        raise RuntimeError("Production pipelines are not activated")
    if os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("Production credentials require a manual main-branch workflow")
    endpoint = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    if not endpoint.startswith("https://"):
        raise RuntimeError("OIDC endpoint must use HTTPS")
    endpoint += ("&" if "?" in endpoint else "?") + "audience=sts.amazonaws.com"
    with urlopen(Request(endpoint, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}), timeout=30) as response:
        token = json.load(response)["value"]
    # The token is on stdin, never in command arguments, files or exception messages.
    result = subprocess.run(["aws", "sts", "assume-role-with-web-identity", "--cli-input-json", "file:///dev/stdin",
                             "--output", "json", "--no-cli-pager"], input=json.dumps({
        "RoleArn": role, "RoleSessionName": "production-" + os.environ["GITHUB_RUN_ID"],
        "WebIdentityToken": token, "DurationSeconds": 3600}), capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Production OIDC role assumption failed; check role trust and environment configuration")
    credentials = json.loads(result.stdout)["Credentials"]
    for env_name, field in (("AWS_ACCESS_KEY_ID", "AccessKeyId"), ("AWS_SECRET_ACCESS_KEY", "SecretAccessKey"),
                            ("AWS_SESSION_TOKEN", "SessionToken")):
        value = credentials[field]
        print("::add-mask::" + value, flush=True)
        os.environ[env_name] = value
    _expires = time.time() + 3000


if __name__ == "__main__":
    refresh()
