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
