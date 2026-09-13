#!/usr/bin/env python3
"""
pii_lint.py — Velnor PII leakage scanner for telemetry calls · T-W0-011

Scans Python (.py) and Go (.go) source files for patterns that suggest
PII values being passed directly into log, span, or metric calls.

Two families of rule:
  LINE rule (T-W0-011, unchanged): a line that looks like a telemetry call AND
      contains `<pii-field>=` anywhere on it.
  CALL rules (LE-553): inside the ARGUMENT LIST of a telemetry call -- which may
      span many lines -- a PII name appearing as
        a. a quoted key or attribute name:   "email", "user.email", "invited_email"
        b. an identifier or attribute chain: email, user.email, row.InvitedEmail
        c. the `<pii-field>=` form, now also on a call's continuation lines.
      String literals and comments are tokenised away first, so prose such as
      "welcome email sent" never counts as a name. See the LE-553 block below.

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
    - CALL rules only: a PII name wrapped in a sanitising call inside the
      telemetry call is not flagged -- e.g. emailDomain(req.OperatorEmail),
      bool(re.search(EMAIL_RE, msg)), len(emails). See SANITIZER_WORDS.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from bisect import bisect_right
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
# Go fmt.Sprintf returns a string; it emits nothing. It has always been in the
# sink list below and the LINE rule keeps it there (test 11's SQL fixture
# depends on it). The CALL rules skip it: over the fleet its only new hit was
# velnor-admin-api tenant_provisioner.go building the SES welcome-email template
# from req.OperatorEmail -- the email body, not a log line.
FMT_SPRINTF_PATTERN = re.compile(r'\bfmt\s*\.\s*Sprintf\s*\(', re.IGNORECASE)

TELEMETRY_CALL_PATTERNS: list[re.Pattern[str]] = [
    # Python: logging / structlog / stdlib
    # (LE-553: `_*` also admits the PEP 8 module-private `_logger` / `_log`.)
    re.compile(r'\b_*(log|logger|logging)\s*\.\s*(debug|info|warning|error|critical|exception|msg|event)\s*\(', re.IGNORECASE),
    # Python: OpenTelemetry span attributes
    re.compile(r'\bspan\s*\.\s*(set_attribute|add_event)\s*\(', re.IGNORECASE),
    # Python: metrics / counters
    re.compile(r'\b(counter|histogram|gauge)\s*\.\s*(add|record|observe)\s*\(', re.IGNORECASE),
    # Python: print (treat as low-confidence log sink)
    re.compile(r'\bprint\s*\(', re.IGNORECASE),
    # Go: log.Printf / log.Println / zap / zerolog / logrus style
    re.compile(r'\b(log|logger|zap|zerolog|logrus)\s*[\.\(]', re.IGNORECASE),
    # Go: fmt.Print* (treat as log sink)
    re.compile(r'\bfmt\s*\.\s*(Print|Printf|Println|Fprintf)\s*\(', re.IGNORECASE),
    # Go: fmt.Sprintf -- a FORMATTER, kept for the LINE rule only (see
    # FMT_SPRINTF_PATTERN below for why the CALL rules skip it).
    FMT_SPRINTF_PATTERN,
    # Go: span.SetAttributes
    re.compile(r'\bspan\s*\.\s*SetAttributes?\s*\(', re.IGNORECASE),
    # Generic: any function call that ends in Log, Logf, Info, Warn, Error, Debug
    re.compile(r'\b\w+(Log|Logf|Info|Warn|Warning|Error|Debug|Trace|Event|Emit)\s*[\.\(]', re.IGNORECASE),
    # --- LE-553 additions (measured over all 12 caller repos, 2026-09-13) ---
    # Go log/slog (445 fleet calls) needs no pattern of its own: the generic
    # pattern above matches `slog.` as `s` + `Log` + `.` under IGNORECASE. An
    # explicit slog pattern was written and removed -- mutation testing showed
    # it was dead (deleting it failed no test). The slog cases in
    # test_pii_lint.py pin the behaviour, so tightening the generic pattern
    # cannot drop slog silently.
    # OpenTelemetry attribute / event calls on ANY receiver. The `span.` patterns
    # above miss `root_span.set_attribute(` (7 fleet calls), `set_attributes(`,
    # and `trace.get_current_span().set_attribute(`.
    re.compile(r'\.\s*(set_attributes?|SetAttributes?|add_event|AddEvent|record_exception|RecordError)\s*\(', re.IGNORECASE),
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


CALL_SINK_PATTERNS: list[re.Pattern[str]] = [
    p for p in TELEMETRY_CALL_PATTERNS if p is not FMT_SPRINTF_PATTERN
]


# ---------------------------------------------------------------------------
# LE-553 · CALL rules
#
# WHY. Until LE-553 the only rule was `<field>=` on a telemetry line, so the
# idiomatic leak shapes all passed: Go slog key/value pairs
# (slog.Info("signup", "email", email)), Python positional args
# (logger.info("signup %s", user.email)) and span attributes with a quoted key
# (span.set_attribute("user.email", user.email)). And because gofmt and black
# break long calls across lines, 885 of the fleet's 2271 telemetry-call lines
# (39%, measured 2026-09-13) open a call whose arguments sit on lines a
# per-line scanner never sees. That is where the fleet's first real hit was:
# velnor-operator-api team_invites.go, `"invited_email", row.InvitedEmail,` on
# the fourth line of a slog.InfoContext call.
#
# HOW. Each file is tokenised (Go or Python) into code / string / comment. For
# every telemetry call found in CODE, the argument list is taken up to its
# balanced `)` -- across lines, and through chained calls such as
# logger.With(...).Info(...) -- and three things are looked for inside it:
#   a. a key-shaped string literal that is a PII name ("email", "user.email");
#   b. an identifier / attribute chain that is a PII name (user.email), or
#      whose RECEIVER is one (user.email.lower());
#   c. `<pii-field>=` on any line of the call, comments excluded.
#
# WHAT COUNTS AS A PII NAME. Its words (split on . _ - and camelCase) must END
# with a term in PII_NAME_TERMS: invited_email, OperatorEmail, client_secret
# and user.email do; email_verified, secret_id, secret_source and emailDomain
# do not. The suffix rule separates a PII VALUE from metadata ABOUT one. A
# leading has/is/num/count marks a predicate or tally, not a value
# ("has_email": bool(...) in velnor-chat-api).
#
# SANITISERS. A PII name inside a call whose name has a word in
# SANITIZER_WORDS -- emailDomain(...), bool(...), len(...), hash_email(...) --
# is not flagged. Any OTHER wrapper still is: str(user.email),
# strings.ToLower(u.Email) and fmt.Sprint(u.Email) all carry the value.
# ---------------------------------------------------------------------------
PII_NAME_TERMS: tuple[tuple[str, ...], ...] = (
    ('email',), ('emails',), ('e', 'mail'), ('email', 'address'), ('email', 'addr'),
    ('phone',), ('phones',), ('phone', 'number'),
    ('ssn',),
    ('credit', 'card'), ('card', 'number'),
    ('password',), ('passwords',), ('passwd',), ('password', 'hash'),
    ('secret',), ('secrets',), ('secret', 'key'), ('secret', 'value'),
    # Beyond the original seven. Names and date of birth are member PII in a
    # gym/studio product by any definition; API keys and bearer tokens are
    # credentials of exactly the class `password` and `secret` already cover.
    ('date', 'of', 'birth'), ('dob',),
    ('first', 'name'), ('last', 'name'), ('full', 'name'),
    ('api', 'key'), ('access', 'token'), ('refresh', 'token'), ('id', 'token'),
)

PREDICATE_PREFIXES = frozenset({'has', 'is', 'num', 'count'})

SANITIZER_WORDS = frozenset({
    'redact', 'redacted', 'mask', 'masked', 'hash', 'hashed', 'sha', 'hmac',
    'digest', 'domain', 'len', 'bool', 'fingerprint', 'anonymize',
    'pseudonymize', 'tokenize',
})

_WORD_RE = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+')
_KEY_SHAPED = re.compile(r'[A-Za-z_][A-Za-z0-9_.\-]*')
_PY_STRING_START = re.compile(r'([rRbBuUfF]{0,2})("""|\'\'\'|"|\')')
_METHOD_OPEN = re.compile(r'\s*\w+\s*\(')
_CHAIN_OPEN = re.compile(r'\s*\.\s*\w+\s*\(')
_CALLEE_BEFORE = re.compile(r'([A-Za-z_][\w.]*)\s*\Z')
_DEF_BEFORE = re.compile(r'\b(func|def)\s+(\([^()]*\)\s*)?\Z')
_IDENT_CHAIN = re.compile(r'(?<![\w.])[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*')
MAX_CALL_CHARS = 20000


def name_words(name: str) -> list[str]:
    """Split a name into lower-case words on . _ - and camelCase boundaries."""
    return [w.lower() for w in _WORD_RE.findall(name)]


def is_pii_name(name: str) -> bool:
    """True if `name` names a PII value (its words END with a PII term)."""
    words = name_words(name)
    if not words or words[0] in PREDICATE_PREFIXES:
        return False
    return any(
        len(words) >= len(term) and tuple(words[-len(term):]) == term
        for term in PII_NAME_TERMS
    )


class Masked(NamedTuple):
    code: str                        # comments AND string literals blanked
    nocomment: str                   # comments blanked, string literals kept
    strings: list[tuple[int, str]]   # (offset of the opening quote, body)


def _blank(buf: list[str], start: int, end: int) -> None:
    for k in range(start, end):
        if buf[k] != '\n':
            buf[k] = ' '


def _scan_quoted(text: str, k: int, quote: str, multiline: bool) -> int:
    """Offset just past the closing `quote`, scanning from body offset k."""
    n = len(text)
    while k < n:
        if text[k] == '\\':
            k += 2
            continue
        if text.startswith(quote, k):
            return k + len(quote)
        if text[k] == '\n' and not multiline:
            return k
        k += 1
    return n


def _unmask_fstring_fields(text: str, start: int, end: int,
                           code: list[str], strings: list[tuple[int, str]]) -> None:
    """Put the {expression} parts of an f-string back into `code`: they are
    code, not prose -- logger.info(f"signup {user.email}") leaks user.email."""
    k = start
    while k < end:
        if text.startswith('{{', k):
            k += 2
            continue
        if text[k] != '{':
            k += 1
            continue
        depth, j = 1, k + 1
        while j < end and depth:
            ch = text[j]
            if ch in '"\'':
                q_end = _scan_quoted(text, j + 1, ch, multiline=False)
                strings.append((j, text[j + 1:q_end - 1]))
                j = q_end
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            j += 1
        for x in range(k + 1, j - 1):
            code[x] = text[x]
        k = j


def mask_source(text: str, is_go: bool) -> Masked:
    """Tokenise Go or Python source just far enough to tell code from strings
    and comments. Offsets and newlines are preserved in every view."""
    code, nocomment = list(text), list(text)
    strings: list[tuple[int, str]] = []
    n, i = len(text), 0
    while i < n:
        c = text[i]
        if (is_go and text.startswith('//', i)) or (not is_go and c == '#'):
            j = text.find('\n', i)
            j = n if j < 0 else j
            _blank(code, i, j)
            _blank(nocomment, i, j)
            i = j
            continue
        if is_go and text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            _blank(code, i, j)
            _blank(nocomment, i, j)
            i = j
            continue
        if is_go and c in '`"\'':
            if c == '`':
                j = text.find('`', i + 1)
                j = n if j < 0 else j + 1
            else:
                j = _scan_quoted(text, i + 1, c, multiline=False)
            strings.append((i, text[i + 1:j - 1]))
            _blank(code, i, j)
            i = j
            continue
        if not is_go and c in '"\'rRbBuUfF' and (
                i == 0 or not (text[i - 1].isalnum() or text[i - 1] == '_')):
            m = _PY_STRING_START.match(text, i)
            if m:
                quote = m.group(2)
                j = _scan_quoted(text, m.end(), quote, multiline=len(quote) == 3)
                body_end = j - len(quote) if text.startswith(quote, j - len(quote)) else j
                strings.append((i, text[m.end():body_end]))
                _blank(code, i, j)
                if 'f' in m.group(1).lower():
                    _unmask_fstring_fields(text, m.end(), body_end, code, strings)
                i = j
                continue
        i += 1
    strings.sort()
    return Masked(''.join(code), ''.join(nocomment), strings)


def _close_paren(code: str, pos: int) -> int | None:
    depth = 1
    for k in range(pos, min(len(code), pos + MAX_CALL_CHARS)):
        ch = code[k]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return k
    return None


def call_arg_spans(code: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the argument list of every telemetry call in
    `code`, following chained calls. Definitions (func/def) are not calls."""
    spans: set[tuple[int, int]] = set()
    for pat in CALL_SINK_PATTERNS:
        for m in pat.finditer(code):
            line_start = code.rfind('\n', 0, m.start()) + 1
            if _DEF_BEFORE.search(code[line_start:m.start()]):
                continue
            pos = m.end()
            if code[pos - 1] != '(':
                opener = _METHOD_OPEN.match(code, pos)
                if not opener:
                    continue
                pos = opener.end()
            while True:
                end = _close_paren(code, pos)
                if end is None:
                    break
                spans.add((pos, end))
                chained = _CHAIN_OPEN.match(code, end + 1)
                if not chained:
                    break
                pos = chained.end()
    return sorted(spans)


def _is_sanitized(code: str, start: int, pos: int) -> bool:
    """True if `pos` sits inside a call (opened after `start`) whose name
    contains a SANITIZER_WORDS word, e.g. emailDomain( or bool(."""
    stack: list[int] = []
    for k in range(start, pos):
        if code[k] == '(':
            stack.append(k)
        elif code[k] == ')' and stack:
            stack.pop()
    for k in stack:
        callee = _CALLEE_BEFORE.search(code[max(0, k - 200):k])
        if callee and SANITIZER_WORDS.intersection(name_words(callee.group(1))):
            return True
    return False


def _call_findings(masked: Masked, start: int, end: int) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    code = masked.code
    # a. key-shaped string literals
    lo = bisect_right(masked.strings, (start, ''))
    for off, body in masked.strings[lo:]:
        if off >= end:
            break
        if (_KEY_SHAPED.fullmatch(body) and is_pii_name(body)
                and not _is_sanitized(code, start, off)):
            found.append((off, f'quoted PII key "{body}" in a telemetry call'))
    # b. identifier / attribute chains
    for m in _IDENT_CHAIN.finditer(code, start, end):
        chain = [part.strip() for part in m.group(0).split('.')]
        if code[m.end():end].lstrip().startswith('('):
            chain = chain[:-1]   # a callee: judge its receiver instead
        if not chain or not is_pii_name(chain[-1]):
            continue
        if _is_sanitized(code, start, m.start()):
            continue
        found.append((m.start(), f'PII identifier `{".".join(chain)}` in a telemetry call'))
    # c. <field>= on every line of the call, comments excluded
    for pat in PII_FIELD_PATTERNS:
        for m in pat.finditer(masked.nocomment, start, end):
            found.append((m.start(), pat.pattern))
    return found


def scan_calls(path: Path, text: str) -> list[Violation]:
    """Apply the LE-553 CALL rules to one file's text."""
    masked = mask_source(text, is_go=path.suffix == '.go')
    line_starts = [0] + [k + 1 for k, ch in enumerate(text) if ch == '\n']
    raw_lines = text.split('\n')
    out: list[Violation] = []
    for start, end in call_arg_spans(masked.code):
        for off, desc in _call_findings(masked, start, end):
            lineno = bisect_right(line_starts, off)
            out.append(Violation(str(path), lineno, raw_lines[lineno - 1].strip(), desc))
    return out


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

    # LE-553 CALL rules, one report per line: a line the LINE rule already
    # flagged is not reported twice.
    flagged = {v.lineno for v in violations}
    for v in scan_calls(path, text):
        if v.lineno not in flagged:
            flagged.add(v.lineno)
            violations.append(v)
    violations.sort(key=lambda v: v.lineno)
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
