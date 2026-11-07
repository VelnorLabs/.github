#!/usr/bin/env python3
"""Compare recorded release manifests offline; never infer live deployment state."""
import argparse
import json
from pathlib import Path

from pipeline import GateError, REPO, SHA, digest, read_json, require


def validate_snapshot(value):
    require(value.get("schema_version") == 1, "unsupported snapshot schema")
    require(isinstance(value.get("components"), dict) and value["components"], "snapshot needs components")
    for name, component in value["components"].items():
        require(REPO.fullmatch(component.get("repository", "")), f"invalid repository: {name}")
        source = component.get("source_sha")
        require(source is None or SHA.fullmatch(source), f"invalid SHA: {name}")


def compare(base, candidate, initial=False):
    require((base is None) == initial, "choose a baseline or explicit initial deployment")
    validate_snapshot(candidate)
    if base is not None:
        validate_snapshot(base)
    previous = base or {"components": {}, "operations": {}, "stages": {}}
    changes = []
    for name in sorted(set(previous["components"]) | set(candidate["components"])):
        old, new = previous["components"].get(name), candidate["components"].get(name)
        old_sha = old.get("source_sha") if old else None
        new_sha = new.get("source_sha") if new else None
        unknown = (old is not None and (old_sha is None or old.get("artifacts") is None)) or (
            new is not None and (new_sha is None or new.get("artifacts") is None))
        if unknown:
            state = "unresolved"
        elif old is None:
            state = "added"
        elif new is None:
            state = "removed"
        elif old["repository"] != new["repository"]:
            state = "repository-changed"
        elif old_sha != new_sha:
            state = "source-changed"
        elif old.get("artifacts") != new.get("artifacts"):
            state = "artifact-changed"
        else:
            state = "unchanged"
        link = None
        if old and new and old_sha and new_sha and old["repository"] == new["repository"] and old_sha != new_sha:
            link = f"https://github.com/{new['repository']}/compare/{old_sha}...{new_sha}"
        changes.append({"component": name, "status": state, "from_sha": old_sha, "to_sha": new_sha,
                        "artifacts_changed": (old or {}).get("artifacts") != (new or {}).get("artifacts"), "compare_url": link})
    operations = []
    before, after = previous.get("operations", {}), candidate.get("operations", {})
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is None or new is None or old != new:
            operations.append({"binding": name, "status": "unresolved" if (name in before and old is None) or
                (name in after and new is None) else "changed", "before_sha256": digest(old) if old is not None else None,
                "after_sha256": digest(new) if new is not None else None})
    return {"schema_version": 1, "baseline": "initial-deployment" if initial else base.get("release_id", "unnamed"),
            "candidate": candidate.get("release_id", "unnamed"), "baseline_sha256": digest(base) if base else None,
            "candidate_sha256": digest(candidate), "components": changes, "operations": operations,
            "stage_order_changed": previous.get("stages") != candidate.get("stages"),
            "limitations": "Recorded manifest comparison only. Verify live inventory and drift separately. Compare links do not establish ancestry, PR inclusion or rollout safety."}


def markdown(report):
    lines = ["# Release delta", "", f"Baseline: {report['baseline']}", f"Candidate: {report['candidate']}", "",
             "| Component | Change | From | To | Source diff |", "| --- | --- | --- | --- | --- |"]
    for item in report["components"]:
        link = f"[Compare]({item['compare_url']})" if item["compare_url"] else "—"
        lines.append(f"| {item['component']} | {item['status']} | {item['from_sha'] or 'unknown/absent'} | {item['to_sha'] or 'unknown/absent'} | {link} |")
    lines += ["", "## Deployment/configuration changes"]
    lines += [f"- {o['binding']}: {o['status']} (compare operation objects in the manifests)." for o in report["operations"]]
    if not report["operations"]:
        lines.append("- No recorded operation changes.")
    lines += [f"- Stage order changed: {report['stage_order_changed']}", "", report["limitations"]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base", type=Path)
    group.add_argument("--initial", action="store_true")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    report = compare(read_json(args.base) if args.base else None, read_json(args.candidate), args.initial)
    print(json.dumps(report, indent=2) if args.format == "json" else markdown(report), end="\n")


if __name__ == "__main__":
    try:
        main()
    except (GateError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"Cannot compare release manifests: {exc}")
