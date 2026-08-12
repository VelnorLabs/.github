#!/usr/bin/env python3
"""
pii_lint.py — Velnor PII leakage scanner for telemetry calls · T-W0-011

Scans Python (.py) and Go (.go) source files for patterns that suggest
PII values being passed directly into log, span, or metric calls.

Exit 0: clean (no violations found)
Exit 1: violations found (prints offending file + line number + content)

Usage:
    python3 scripts/pii_lint.py [--root <path>]

Options:
    --root PATH   Root directory to scan (default: current directory)

Allowlist:
    - Files under any `tests/` directory are skipped.
    - Go test files (`*_test.go`) are skipped — see GO_TEST_FILE_SUFFIX below.
    - Files containing the comment `# pii-lint: ignore` are skipped entirely.
    - Dot-prefixed directories are never descended into (see scan_tree).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# PII field name patterns — these indicate the *key* name in a call, not the
# value.  We flag assignments like email=..., phone=..., etc. appearing inside
# recognised telemetry call contexts.
#
# Each pattern is a compiled regex that matches a *line* of source text.
# We use word-boundary anchors so `password_hash` also triggers (intentional —
# any key containing a PII keyword is suspicious).
# ---------------------------------------------------------------------------
PII_FIELD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'\b(email|e_mail)\s*=', re.IGNORECASE),
    re.compile(r'\bphone\s*=', re.IGNORECASE),
    re.compile(r'\bssn\s*=', re.IGNORECASE),
    re.compile(r'\bcredit_card\s*=', re.IGNORECASE),
    re.compile(r'\bpassword\s*=', re.IGNORECASE),
    re.compile(r'\bsecret\s*=', re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Telemetry call patterns — we only flag PII fields when they appear *within*
# lines that look like a logging, span, or metric call.  This reduces false
# positives on legitimate model/DB code.
# ---------------------------------------------------------------------------
TELEMETRY_CALL_PATTERNS: list[re.Pattern[str]] = [
    # Python: logging / structlog / stdlib
    re.compile(r'\b(log|logger|logging)\s*\.\s*(debug|info|warning|error|critical|exception|msg|event)\s*\(', re.IGNORECASE),
    # Python: OpenTelemetry span attributes
    re.compile(r'\bspan\s*\.\s*(set_attribute|add_event)\s*\(', re.IGNORECASE),
    # Python: metrics / counters
    re.compile(r'\b(counter|histogram|gauge)\s*\.\s*(add|record|observe)\s*\(', re.IGNORECASE),
    # Python: print (treat as low-confidence log sink)
    re.compile(r'\bprint\s*\(', re.IGNORECASE),
    # Go: log.Printf / log.Println / zap / zerolog / logrus style
    re.compile(r'\b(log|logger|zap|zerolog|logrus)\s*[\.\(]', re.IGNORECASE),
    # Go: fmt.Print* (treat as log sink)
    re.compile(r'\bfmt\s*\.\s*(Print|Printf|Println|Fprintf|Sprintf)\s*\(', re.IGNORECASE),
    # Go: span.SetAttributes
    re.compile(r'\bspan\s*\.\s*SetAttributes?\s*\(', re.IGNORECASE),
    # Generic: any function call that ends in Log, Logf, Info, Warn, Error, Debug
    re.compile(r'\b\w+(Log|Logf|Info|Warn|Warning|Error|Debug|Trace|Event|Emit)\s*[\.\(]', re.IGNORECASE),
]

FILE_EXTENSIONS = {'.py', '.go'}

IGNORE_COMMENT = '# pii-lint: ignore'

TESTS_DIR_PATTERN = re.compile(r'(^|[\\/])tests[\\/]')

# ---------------------------------------------------------------------------
# Go has no `tests/` directory convention. The toolchain identifies test code by
# the `_test.go` FILE suffix, and the compiler excludes those files from every
# non-test build — nothing inside one can reach a production log sink. Skipping
# them applies the rule the `tests/` skip already states to the layout Go
# actually uses; it is not a new exemption.
#
# Deliberately NOT extended to Python's `test_*.py` naming. That is a pytest
# COLLECTION convention, not a compiler boundary: a module named
# `test_helpers.py` can be imported and run by shipped code, so exempting it by
# filename would be a real hole. Python test code stays exempt only by living
# under `tests/`.
#
# WHY THIS EXISTS AT ALL. This scanner has never run in any service repo except
# velnor-chat-api: _ci-template.yml gated it on `[ -f scripts/pii_lint.py ]` in
# the CALLING repo, and only chat-api ever vendored a copy. The same change that
# adds this suffix makes that fallback real, so the scanner starts running in 10
# more repos at once. Before doing that, it was run locally over
# `git archive origin/main` of all 11 repos that carry a real `uses:` line for
# the shared template (2026-08-11; three further repos only MENTION it in
# comments and were counted as callers on a first pass — grep for the `uses:`,
# not the filename). Exactly one violation existed fleet-wide and it was a false
# positive of precisely this shape — velnor-plane-api
# internal/applier/applier_db_test.go:226, a SQL fixture:
#     fmt.Sprintf(`UPDATE %s.members SET phone = $1 WHERE id = $2`, fx.schema),
# where `fmt.Sprintf` matches a log-sink pattern and `phone =` matches a PII key
# name. Without this rule, turning the fallback on would have turned that repo
# red on its next PR, for a line that logs nothing.
# ---------------------------------------------------------------------------
GO_TEST_FILE_SUFFIX = '_test.go'


class Violation(NamedTuple):
    path: str
    lineno: int
    line: str
    pattern: str


def is_ignored_path(path: Path, root: Path) -> bool:
    """Return True if this file should be skipped entirely."""
    rel = path.relative_to(root)
    # Skip files under any tests/ directory
    if TESTS_DIR_PATTERN.search(str(rel)):
        return True
    # Skip Go test files wherever they live — Go puts test code beside the code
    # it tests rather than in a tests/ directory (see GO_TEST_FILE_SUFFIX).
    if path.name.endswith(GO_TEST_FILE_SUFFIX):
        return True
    return False


def has_ignore_comment(path: Path) -> bool:
    """Return True if the file contains the pii-lint: ignore directive."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
        return IGNORE_COMMENT in text
    except OSError:
        return False


def line_matches_telemetry(line: str) -> bool:
    """Return True if the line looks like a telemetry / log call."""
    return any(pat.search(line) for pat in TELEMETRY_CALL_PATTERNS)


def line_matches_pii(line: str) -> str | None:
    """Return the matching pattern description if the line contains a PII field, else None."""
    for pat in PII_FIELD_PATTERNS:
        if pat.search(line):
            return pat.pattern
    return None


def scan_file(path: Path) -> list[Violation]:
    """Scan a single source file and return any PII violations found."""
    violations: list[Violation] = []
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        print(f"WARNING: cannot read {path}: {exc}", file=sys.stderr)
        return violations

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        # Only flag lines that are both PII-key-shaped AND inside a telemetry call
        matched_pii = line_matches_pii(line)
        if matched_pii and line_matches_telemetry(line):
            violations.append(Violation(
                path=str(path),
                lineno=lineno,
                line=line.strip(),
                pattern=matched_pii,
            ))

    return violations


def scan_tree(root: Path) -> list[Violation]:
    """Walk the directory tree and scan all eligible source files."""
    all_violations: list[Violation] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune hidden dirs, vendor dirs, venv dirs, __pycache__, node_modules
        #
        # THE DOT-PREFIX PRUNE IS LOAD-BEARING FOR CI, not just tidiness.
        # _ci-template.yml checks this repo's copy of the scanner out into
        # `.velnor-ci-shared/` inside the caller's workspace (actions/checkout
        # refuses a path outside $GITHUB_WORKSPACE) and then scans `--root .`,
        # so the scanner's own source sits inside the tree it walks. This prune
        # is the only thing keeping it out.
        #
        # Measured 2026-08-11, not assumed: a non-dot copy of this repo dropped
        # into a scanned tree currently exits 0 — but ONLY because
        # scripts/test_pii_lint.py contains the literal string
        # `# pii-lint: ignore` inside a fixture for the ignore-directive test,
        # which makes has_ignore_comment() skip the whole file. Scan that file
        # with the directive check bypassed and it yields 15 violations, since
        # its fixtures are deliberate leaks like
        # `log.Printf("login: email=%s", email)`. So the green result on a
        # non-dot path is an accident of one test fixture, one edit away from
        # failing every repo in the org on the scanner's own test data.
        # test_pii_lint.py asserts this prune with its own violating fixture
        # plus a non-dot control; do not "simplify" it away.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith('.')
            and d not in ('vendor', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build')
        ]

        for filename in filenames:
            filepath = Path(dirpath) / filename
            if filepath.suffix not in FILE_EXTENSIONS:
                continue
            if is_ignored_path(filepath, root):
                continue
            if has_ignore_comment(filepath):
                continue
            violations = scan_file(filepath)
            all_violations.extend(violations)

    return all_violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Scan source files for PII leakage in telemetry calls.',
    )
    parser.add_argument(
        '--root',
        default='.',
        help='Root directory to scan (default: current directory)',
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: --root path does not exist or is not a directory: {root}", file=sys.stderr)
        return 1

    print(f"pii_lint: scanning {root}", file=sys.stderr)
    violations = scan_tree(root)

    if not violations:
        print("pii_lint: OK — no PII leakage patterns found in telemetry calls.", file=sys.stderr)
        return 0

    print(f"\npii_lint: FAIL — {len(violations)} violation(s) found:\n", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.lineno}: {v.line}", file=sys.stderr)
        print(f"    matched pattern: {v.pattern}", file=sys.stderr)
    print(
        "\nFix: remove PII values from log/span/metric calls, or add '# pii-lint: ignore'"
        " at the top of the file if this is intentional (e.g. auth service internals).",
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())
