"""
test_pii_lint.py — pytest tests for pii_lint.py · T-W0-011

Tests:
  1. Clean file → exit 0
  2. File with email= in a log call → exit 1
  3. File with # pii-lint: ignore → exit 0
  4. File in tests/ directory → exit 0
  5. File with password= in a log call → exit 1
  6. File with email= but NOT in a telemetry call → exit 0 (not flagged)
  7. File with secret= in a span.set_attribute call → exit 1
  8. Go file with email= in log.Printf → exit 1
  9. Go file with email= in log.Printf but in tests/ → exit 0
 10. Go *_test.go file beside the code it tests → exit 0
 11. The real velnor-plane-api false positive → exit 0 in _test.go, 1 elsewhere
 12. Dot-prefixed directories are not descended into (CI depends on this)
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parent / 'pii_lint.py'


def run_lint(root: Path) -> subprocess.CompletedProcess:
    """Run pii_lint.py --root <root> and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), '--root', str(root)],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Helper: write a source file inside a temp tree
# ---------------------------------------------------------------------------

def write_file(root: Path, rel_path: str, content: str) -> Path:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content))
    return target


# ---------------------------------------------------------------------------
# Test 1: Clean Python file — no PII patterns → exit 0
# ---------------------------------------------------------------------------

def test_clean_file_exits_0(tmp_path):
    write_file(tmp_path, 'src/service.py', """\
        import logging
        logger = logging.getLogger(__name__)

        def process(user_id: str, amount: float):
            logger.info("processing transaction", extra={"user_id": user_id, "amount": amount})
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Test 2: File with email= in a log call → exit 1
# ---------------------------------------------------------------------------

def test_email_in_log_call_exits_1(tmp_path):
    write_file(tmp_path, 'src/auth.py', """\
        import logging
        logger = logging.getLogger(__name__)

        def login(email: str, password_hash: str):
            logger.info("login attempt", email=email, user="anon")
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}\nstderr:\n{result.stderr}"
    assert 'email' in result.stderr.lower() or 'src/auth.py' in result.stderr


# ---------------------------------------------------------------------------
# Test 3: File with `# pii-lint: ignore` → exit 0
# ---------------------------------------------------------------------------

def test_ignore_comment_exits_0(tmp_path):
    write_file(tmp_path, 'src/internal_auth.py', """\
        # pii-lint: ignore
        import logging
        logger = logging.getLogger(__name__)

        def debug_user(email: str):
            logger.debug("user debug", email=email)
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, f"Expected exit 0 (ignored file), got {result.returncode}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Test 4: File in tests/ directory → exit 0 (tests/ is allowlisted)
# ---------------------------------------------------------------------------

def test_file_in_tests_dir_exits_0(tmp_path):
    write_file(tmp_path, 'tests/test_auth.py', """\
        import logging
        logger = logging.getLogger(__name__)

        def test_login_with_pii():
            logger.info("test: checking login", email="user@example.com", phone="555-1234")
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        f"Expected exit 0 (tests/ dir is allowlisted), got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 5: File with password= in a log call → exit 1
# ---------------------------------------------------------------------------

def test_password_in_log_call_exits_1(tmp_path):
    write_file(tmp_path, 'src/session.py', """\
        import logging
        log = logging.getLogger(__name__)

        def create_session(user_id: str, password: str):
            log.warning("creating session", user_id=user_id, password=password)
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Test 6: File with email= but NOT inside a telemetry call → exit 0
# (Assignment in a function signature or DB model, not a log/span call)
# ---------------------------------------------------------------------------

def test_email_outside_telemetry_exits_0(tmp_path):
    write_file(tmp_path, 'src/models.py', """\
        from dataclasses import dataclass

        @dataclass
        class User:
            user_id: str
            email: str = ""

        def save_user(user_id: str, email: str):
            db.save(user_id=user_id, email=email)
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        f"Expected exit 0 (email= not in telemetry call), got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 7: File with secret= in a span.set_attribute call → exit 1
# ---------------------------------------------------------------------------

def test_secret_in_span_exits_1(tmp_path):
    write_file(tmp_path, 'src/tracer.py', """\
        from opentelemetry import trace

        tracer = trace.get_tracer(__name__)

        def traced_request(api_key: str, secret: str):
            with tracer.start_as_current_span("api_call") as span:
                span.set_attribute("api.secret", secret)
                span.set_attribute("request.secret=", api_key)
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 1, f"Expected exit 1 (secret= in span), got {result.returncode}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Test 8: Go file with email= in log.Printf → exit 1
# ---------------------------------------------------------------------------

def test_go_email_in_log_exits_1(tmp_path):
    write_file(tmp_path, 'cmd/server/main.go', """\
        package main

        import "log"

        func handleLogin(email string, userID string) {
            log.Printf("login: email=%s user=%s", email, userID)
        }
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 1, (
        f"Expected exit 1 (Go email= in log.Printf), got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 9: Go file with email= in log.Printf but inside tests/ → exit 0
# ---------------------------------------------------------------------------

def test_go_email_in_tests_dir_exits_0(tmp_path):
    write_file(tmp_path, 'tests/login_test.go', """\
        package tests

        import "log"

        func TestLogin(email string) {
            log.Printf("test login: email=%s", email)
        }
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        f"Expected exit 0 (Go file in tests/ is allowlisted), got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 10: Go *_test.go file beside the code it tests → exit 0
#
# Test 9 above only covers a Go file inside a tests/ DIRECTORY, which is not
# how Go is laid out — `go test` finds test code by the `_test.go` file suffix
# and it sits next to the package it tests. So test 9 passed while real Go test
# files were being scanned. This is the case that matters in practice.
# ---------------------------------------------------------------------------

def test_go_test_file_suffix_exits_0(tmp_path):
    write_file(tmp_path, 'internal/auth/login_test.go', """\
        package auth

        import "log"

        func TestLogin(t *testing.T) {
            log.Printf("test login: email=%s", "a@b.c")
        }
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        f"Expected exit 0 (*_test.go is allowlisted), got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 11: the exact line that would have turned velnor-plane-api red.
#
# internal/applier/applier_db_test.go:226 — a SQL fixture, verbatim. It is not
# a log call at all: `fmt.Sprintf` matches the log-sink pattern list and
# `phone = $1` matches the PII key-name pattern, and the two coincide inside a
# SQL string. This is the ONLY violation the scanner found across all 13 repos
# that call the shared CI template (measured 2026-08-11, against
# `git archive origin/main` of each).
#
# The second half is the part that keeps the fix honest: the SAME text in a
# non-test .go file must still fail. If the exemption were ever widened from
# the `_test.go` suffix to something content-based, this half goes green and
# tells you.
# ---------------------------------------------------------------------------

PLANE_API_SQL_FIXTURE_LINE = (
    'fmt.Sprintf(`UPDATE %s.members SET phone = $1 WHERE id = $2`, fx.schema),'
)


def test_plane_api_sql_fixture_in_test_file_exits_0(tmp_path):
    write_file(tmp_path, 'internal/applier/applier_db_test.go', f"""\
        package applier

        func seedPhone(fx fixture) {{
            _ = {PLANE_API_SQL_FIXTURE_LINE}
        }}
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        "Expected exit 0 — the velnor-plane-api SQL fixture is in a *_test.go "
        f"file, got {result.returncode}\nstderr:\n{result.stderr}"
    )


def test_plane_api_sql_fixture_in_source_file_exits_1(tmp_path):
    write_file(tmp_path, 'internal/applier/applier_db.go', f"""\
        package applier

        func seedPhone(fx fixture) {{
            _ = {PLANE_API_SQL_FIXTURE_LINE}
        }}
    """)
    result = run_lint(tmp_path)
    assert result.returncode == 1, (
        "Expected exit 1 — the *_test.go exemption must be scoped to the "
        f"filename, not to the line content, got {result.returncode}\nstderr:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 12: dot-prefixed directories are never scanned.
#
# _ci-template.yml checks THIS repo out into `.velnor-ci-shared/` inside the
# caller's workspace and then runs the scanner with `--root .`. actions/checkout
# will not write outside $GITHUB_WORKSPACE, so the shared copy is unavoidably
# inside the tree being scanned, and the dot prefix is the only thing keeping
# the scanner from scanning itself — including this file, whose fixtures above
# are deliberate violations. If this prune is lost, every repo in the org fails
# its PII lint on the scanner's own test data.
#
# The control case (same content, no dot) proves the fixture really is a
# violation, so a green result here can't come from a harmless fixture.
# ---------------------------------------------------------------------------

_VIOLATING_PY = """\
    import logging
    logger = logging.getLogger(__name__)

    def leak(email: str):
        logger.info("signup", email=email)
"""


def test_dot_prefixed_directory_is_not_scanned(tmp_path):
    write_file(tmp_path, '.velnor-ci-shared/scripts/leaky.py', _VIOLATING_PY)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        "Expected exit 0 — .velnor-ci-shared/ is where _ci-template.yml puts "
        f"the shared scanner checkout and must not be scanned, got {result.returncode}"
        f"\nstderr:\n{result.stderr}"
    )


def test_same_file_outside_dot_directory_is_scanned(tmp_path):
    write_file(tmp_path, 'velnor-ci-shared/scripts/leaky.py', _VIOLATING_PY)
    result = run_lint(tmp_path)
    assert result.returncode == 1, (
        "Control for the test above: without the dot the identical file must be "
        f"flagged, or the dot-prune test proves nothing. Got {result.returncode}"
        f"\nstderr:\n{result.stderr}"
    )
