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
 13. LE-553 CALL rules: leak shapes that passed before LE-553 now fail
     (the three LE-553 controls verbatim, plus one case per rule and sink)
 14. LE-553 false-positive controls, each a real line shape from a caller repo
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


# ---------------------------------------------------------------------------
# Test 13: LE-553 — leak shapes the LINE rule never saw.
#
# Before LE-553 the scanner flagged only `<field>=` on a telemetry line. Every
# case below exited 0 against that scanner (measured 2026-09-13); the first
# three are the negative controls LE-553 was filed on, verbatim. Each case
# asserts rc=1 AND the reported `path:line:` AND, where one rule alone must
# fire, that rule's own message fragment — so a mutation that kills one rule
# cannot be masked by another rule catching the same line.
# ---------------------------------------------------------------------------

LE553_LEAKS = [
    # (id, relative path, source, expected `path:line:`, expected fragment or None)
    ('le553-control-go-slog-key-value', 'internal/signup/handler.go', """\
        package signup

        import "log/slog"

        func signup(email string) {
            slog.Info("signup", "email", email)
        }
    """, 'handler.go:6:', None),
    ('le553-control-py-positional-attribute', 'src/signup.py', """\
        import logging
        logger = logging.getLogger(__name__)

        def signup(user):
            logger.info("signup %s", user.email)
    """, 'signup.py:5:', 'PII identifier `user.email`'),
    ('le553-control-py-span-quoted-key', 'src/tracing.py', """\
        def trace_signup(span, user):
            span.set_attribute("user.email", user.email)
    """, 'tracing.py:2:', None),
    ('quoted-key-only', 'src/tracing.py', """\
        def trace_signup(span, value):
            span.set_attribute("user.email", value)
    """, 'tracing.py:2:', 'quoted PII key "user.email"'),
    ('go-multiline-slog-continuation-line', 'internal/handlers/team_invites.go', """\
        package handlers

        func created(r *http.Request, row inviteRow) {
            slog.InfoContext(r.Context(), "team_invite.created",
                "invite_id", row.InviteID,
                "tenant_id", row.TenantID,
                "invited_email", row.InvitedEmail,
                "role", row.Role,
            )
        }
    """, 'team_invites.go:7:', None),
    ('py-fstring-expression', 'src/signup.py', """\
        def signup(logger, user):
            logger.info(f"signup for {user.email} done")
    """, 'signup.py:2:', 'PII identifier `user.email`'),
    ('receiver-of-method-call', 'src/signup.py', """\
        def signup(logger, user):
            logger.info("signup %s", user.email.lower())
    """, 'signup.py:2:', 'PII identifier `user.email`'),
    ('non-sanitizer-wrapper-still-leaks', 'src/signup.py', """\
        def signup(logger, user):
            logger.info("signup %s", str(user.email))
    """, 'signup.py:2:', 'PII identifier `user.email`'),
    # `email=` inside a string on a continuation line: not key-shaped, not an
    # identifier, not a telemetry line — only rule c (field= across the call).
    ('go-multiline-field-equals-continuation', 'cmd/server/login.go', """\
        package main

        func login(addr string) {
            log.Printf(
                "login: email=%s", addr,
            )
        }
    """, 'login.go:5:', r'\b(email|e_mail)\s*='),
    ('root-span-receiver-sink', 'src/tracing.py', """\
        def trace_signup(root_span, value):
            root_span.set_attribute("user.email", value)
    """, 'tracing.py:2:', 'quoted PII key "user.email"'),
    ('credential-identifier', 'src/config.py', """\
        def show(logger, cfg):
            logger.info("loaded %s", cfg.client_secret)
    """, 'config.py:2:', 'PII identifier `cfg.client_secret`'),
    ('go-chained-call-with-then-info', 'internal/signup/chain.go', """\
        package signup

        func signup(logger *slog.Logger, t string, e string) {
            logger.With("tenant", t).Info("signup", "email", e)
        }
    """, 'chain.go:4:', 'quoted PII key "email"'),
    ('py-private-underscore-logger', 'src/signup.py', """\
        class Signup:
            def run(self, user):
                self._logger.info("signup %s", user.email)
    """, 'signup.py:3:', 'PII identifier `user.email`'),
    ('go-zap-field-key', 'internal/signup/zap.go', """\
        package signup

        func signup(logger *zap.Logger, e string) {
            logger.Info("signup", zap.String("email", e))
        }
    """, 'zap.go:4:', 'quoted PII key "email"'),
]


@pytest.mark.parametrize(
    'rel_path,source,where,fragment',
    [pytest.param(p, s, w, f, id=i) for i, p, s, w, f in LE553_LEAKS],
)
def test_le553_leak_shapes_exit_1(tmp_path, rel_path, source, where, fragment):
    write_file(tmp_path, rel_path, source)
    result = run_lint(tmp_path)
    assert result.returncode == 1, (
        f"Expected exit 1 — this leak shape passed before LE-553, got "
        f"{result.returncode}\nstderr:\n{result.stderr}"
    )
    assert where in result.stderr, f"Expected a report at {where!r}\nstderr:\n{result.stderr}"
    if fragment is not None:
        assert fragment in result.stderr, (
            f"Expected the rule message {fragment!r}\nstderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Test 14: LE-553 false-positive controls.
#
# Each is a line shape found in a caller repo's default branch during the
# 2026-09-13 fleet sweep (repo named per case), and each is a line that logs
# no PII. A widening that trips any of them turns that repo red on its next PR.
# ---------------------------------------------------------------------------

LE553_CLEAN = [
    # velnor-admin-api tenant_provisioner.go — "email" as prose in a message.
    ('prose-email-sent-in-message', 'internal/services/provision.go', """\
        package services

        func done(ctx context.Context, domain string) {
            slog.InfoContext(ctx, "finalize: welcome email sent",
                "operator_domain", domain)
        }
    """),
    # velnor-admin-api tenants.go — sanitiser wrapper, plus a comment naming
    # operator_email inside the call.
    ('sanitizer-wrapper-and-comment', 'internal/handlers/tenants.go', """\
        package handlers

        func requested(ctx context.Context, req provisionRequest) {
            slog.InfoContext(ctx, "tenant provisioning requested",
                "slug", req.Slug,
                // operator_email is PII — log only domain portion for debugging.
                "operator_domain", emailDomain(req.OperatorEmail),
            )
        }
    """),
    # velnor-chat-api composites/intent.py — a boolean about an email.
    ('predicate-key-and-bool-wrapper', 'src/composites/intent.py', """\
        import logging
        import re
        logger = logging.getLogger(__name__)

        def near_miss(message):
            logger.info(
                "composite_intent_near_miss",
                extra={
                    "length": len(message),
                    "has_email": bool(re.search(_EMAIL, message)),
                },
            )
    """),
    ('predicate-key-alone', 'src/flags.py', """\
        def report(logger, flag):
            logger.info("signup", extra={"has_email": flag})
    """),
    # velnor-workers cmd/stripe-webhook/main.go — metadata ABOUT a secret.
    ('metadata-suffix-keys', 'cmd/stripe-webhook/main.go', """\
        package main

        func ready(secretID string, err error) {
            slog.Warn("stripe_webhook_secret_fetch_failed",
                "secret_id", secretID,
                "secret_source", secretSource(),
                "err", err)
        }
    """),
    # velnor-admin-api tenant_provisioner.go — fmt.Sprintf builds the SES
    # welcome-email template; it formats a string and logs nothing.
    ('sprintf-formatter-not-a-sink', 'internal/services/welcome.go', """\
        package services

        func template(req provisionRequest, loginURL string) string {
            return fmt.Sprintf(
                `{"operator_name":%q,"login_url":%q}`,
                req.OperatorEmail, loginURL,
            )
        }
    """),
    # A definition whose name matches the generic `\\w+Error(` sink is not a call.
    ('function-definition-not-a-call', 'internal/notify/notify.go', """\
        package notify

        func notifyError(email string) error {
            return nil
        }
    """),
]


@pytest.mark.parametrize(
    'rel_path,source',
    [pytest.param(p, s, id=i) for i, p, s in LE553_CLEAN],
)
def test_le553_false_positive_controls_exit_0(tmp_path, rel_path, source):
    write_file(tmp_path, rel_path, source)
    result = run_lint(tmp_path)
    assert result.returncode == 0, (
        f"Expected exit 0 — this line shape logs no PII, got {result.returncode}"
        f"\nstderr:\n{result.stderr}"
    )
