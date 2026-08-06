#!/usr/bin/env python3
"""
check_workflows.py — structural checks on this repo's own workflow files · LE-374

WHY THIS EXISTS. VelnorLabs/.github is the only repo in the org with branch
protection (it is the only public one — LE-068 covers why the private repos
have none), and its main branch required a status check named `ci` that NO
workflow here ever reported. A required check that never reports can never be
satisfied, so every PR sat at MERGEABLE/BLOCKED and the only way to land
anything was `--admin`, which worked because enforce_admins is false. PRs #32
through #40 all merged that way, with zero reviews.

The harm was never the bypass. It was that admin-bypass became the ONLY workflow
in the one repo where governance is actually enforceable — so the bypass reflex
was trained on the org's most sensitive shared asset, `_deploy-template.yml`,
which every service deploy in the org runs. A gate nobody can pass costs the
same to route around every time, and it makes a real gate indistinguishable
from a broken one.

So `ci` is made REAL rather than removed, and it checks something worth
checking: the workflow files here are consumed BY NAME from every service repo,
and a syntax error in one breaks every repo's CI or deploy. Nothing checked
them before.

Run: python3 scripts/check_workflows.py
"""

from __future__ import annotations

import pathlib
import sys

import yaml

WORKFLOW_DIR = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The context that VelnorLabs/.github's main-branch protection requires. This
# string is LOAD-BEARING: it must equal the name GitHub reports for the job in
# ci.yml, or main is blocked again exactly as it was before LE-374. Renaming
# that job without changing the protection is the whole bug, so it is asserted
# rather than trusted.
REQUIRED_CONTEXT = "ci"

CI_WORKFLOW = "ci.yml"

# ── Which `_`-prefixed workflows are actually TEMPLATES ─────────────────────
#
# The `_` prefix is NOT a reliable signal, which the first run of this script
# proved: `_schema-sla-watch.yml` carries the prefix and is a cron that runs in
# THIS repo, with zero callers anywhere in the org. Inferring "template" from
# the filename would have failed a file that is perfectly correct.
#
# So membership is stated, not guessed — the same allowlist-plus-explicit-
# exemption shape as scripts/check_schema_versions.sh in velnor-schemas
# (LE-352). A `_`-prefixed file in NEITHER list is a hard failure, so adding
# one and forgetting to classify it is loud rather than silently unchecked.
#
# Caller counts below were measured across the org's repos on 2026-08-06. They
# are context for a reader, not something this script verifies — it cannot see
# other repos.
CALLED_BY_OTHER_REPOS = {
    "_ci-template.yml": "every service repo's ci.yml (~94 caller files)",
    "_deploy-template.yml": "every service repo's deploy.yml (~68)",
    "_sdk-bypass-check.yml": "every Go/Python service (~69)",
    "_eval-on-pr.yml": "eval-gated repos (~27)",
    "_design-tokens-gate.yml": "the front-end repos (~5)",
    "_schema-bypass-check.yml": "schema-consuming repos (~3)",
}

SELF_TRIGGERED = {
    "_schema-sla-watch.yml": (
        "cron in THIS repo (schedule + workflow_dispatch), watching velnor-schemas "
        "PRs for reviewer-SLA breaches. Underscore-prefixed by convention drift, "
        "not because anything calls it — zero callers org-wide."
    ),
}

# PyYAML resolves the bare key `on` to the boolean True (YAML 1.1 truthiness),
# so a workflow's trigger block is reachable under either key depending on
# whether it was quoted. Checking one and not the other is a silent no-op.
ON_KEYS = ("on", True)


def triggers(doc: dict) -> dict | list | str | None:
    for key in ON_KEYS:
        if key in doc:
            return doc[key]
    return None


def declares_workflow_call(on) -> bool:
    if isinstance(on, dict):
        return "workflow_call" in on
    if isinstance(on, list):
        return "workflow_call" in on
    return on == "workflow_call"


def check_name(job_id: str, job: dict) -> str:
    """The name GitHub reports for a check run: the job's `name`, or its id."""
    if isinstance(job, dict) and job.get("name"):
        return str(job["name"])
    return job_id


def main() -> int:
    failures: list[str] = []
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))

    if not files:
        print(f"FAIL: no workflow files found under {WORKFLOW_DIR}", file=sys.stderr)
        return 1

    parsed: dict[str, dict] = {}

    # ── 1. Every workflow parses ────────────────────────────────────────────
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            failures.append(f"{path.name}: not valid YAML — {exc}")
            continue
        if not isinstance(doc, dict):
            failures.append(f"{path.name}: top level is {type(doc).__name__}, expected a mapping")
            continue
        parsed[path.name] = doc
        print(f"OK  : {path.name} parses")

    # ── 2. Reusable templates stay callable ─────────────────────────────────
    # A template here is referenced as
    # `uses: VelnorLabs/.github/.github/workflows/_x.yml@main` from service
    # repos. Dropping workflow_call from one does not fail HERE — it fails in
    # every repo that calls it, at the next push, with an error about a
    # workflow that "does not exist". That is the worst possible place to find
    # out, so it is asserted at the source.
    for name in sorted(CALLED_BY_OTHER_REPOS):
        if name not in parsed:
            failures.append(
                f"{name} is listed as called by other repos but does not exist. "
                "Deleting or renaming a template breaks every caller; if it is "
                "genuinely gone, remove it from CALLED_BY_OTHER_REPOS in the "
                "same change that updates the callers."
            )
            continue
        if not declares_workflow_call(triggers(parsed[name])):
            failures.append(
                f"{name}: called by {CALLED_BY_OTHER_REPOS[name]}, but it no "
                "longer declares `on: workflow_call`. Without that trigger every "
                "caller breaks, and none of them break here."
            )
        else:
            print(f"OK  : {name} is callable (workflow_call)")

    # Coverage audit: a `_`-prefixed file in neither list is unclassified, and
    # an unclassified template is one nothing checks (LE-352's failure mode).
    for name in sorted(parsed):
        if not name.startswith("_"):
            continue
        if name in CALLED_BY_OTHER_REPOS or name in SELF_TRIGGERED:
            continue
        failures.append(
            f"{name} is `_`-prefixed but classified nowhere in "
            "scripts/check_workflows.py. Add it to CALLED_BY_OTHER_REPOS (and it "
            "must then keep `on: workflow_call`) or to SELF_TRIGGERED with a "
            "reason. A file on neither list is silently unchecked."
        )

    for name, why in sorted(SELF_TRIGGERED.items()):
        if name in parsed:
            print(f"SKIP: {name} — not a template ({why.split('.')[0]})")

    # ── 3. The required status check actually exists ────────────────────────
    if CI_WORKFLOW not in parsed:
        failures.append(
            f"{CI_WORKFLOW} is missing. Main-branch protection requires the "
            f"status check `{REQUIRED_CONTEXT}`; with no workflow reporting it, "
            "every PR is permanently blocked (LE-374)."
        )
    else:
        jobs = parsed[CI_WORKFLOW].get("jobs") or {}
        names = {check_name(jid, job) for jid, job in jobs.items()}
        if REQUIRED_CONTEXT not in names:
            failures.append(
                f"{CI_WORKFLOW} declares no job reporting as `{REQUIRED_CONTEXT}` "
                f"(found: {sorted(names)}). That string is the required status "
                "check on main — renaming the job without updating branch "
                "protection re-blocks the branch for everyone (LE-374)."
            )
        else:
            print(f"OK  : {CI_WORKFLOW} reports the required context `{REQUIRED_CONTEXT}`")

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        print(f"\ncheck_workflows: {len(failures)} failure(s).", file=sys.stderr)
        return 1

    print(f"check_workflows: {len(files)} workflow file(s) OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
