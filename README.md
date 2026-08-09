# velnor-github-dotfiles · VelnorLabs org .github repo

This is the local representation of the `VelnorLabs/.github` GitHub org repo.
It is pushed to `github.com/VelnorLabs/.github` once an org-admin applies the
branch-protection ruleset and the GitHub OIDC TF is live.

## Layout

```
velnor-github-dotfiles/
├── CODEOWNERS                         # Owners of this .github repo itself
├── .github/
│   └── workflows/
│       └── _ci-template.yml          # Reusable workflow (workflow_call)
├── branch-protection/
│   └── ruleset.tf                    # Org-level branch ruleset (apply once by human org-admin)
└── codeowners-templates/
    ├── CODEOWNERS.schemas             # For velnor-schemas (Q-2 rules)
    └── CODEOWNERS.service             # For velnor-* service repos
```

## Shared CI template usage

In any `VelnorLabs/*` repo, create `.github/workflows/ci.yml`:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  ci:
    uses: VelnorLabs/.github/.github/workflows/_ci-template.yml@main
    with:
      service_name: velnor-plane-api
      image_tag: ${{ github.sha }}
    secrets: inherit
```

That is the entire `ci.yml`. The shared template handles lint, unit tests,
build, Trivy scan, cosign image signing, and OIDC smoke test.

## On-PR eval subset (`_eval-on-pr.yml` · T-W3-020 impl · T-W5-003 promotion)

Reusable Stage-1 eval workflow: the Q-16/Q-17 git-diff resolver
(`velnor-evals/hotfix_subset_resolver.py`) classifies a PR's changed paths as
`ai_harness_intersected` / `infra_only` / `mixed` and the recorded golden-subset
eval (`velnor-evals harnesses/subset_run.py`, deterministic, no model calls)
runs only for intersected (targeted tags) or mixed (full subset) — infra-only
and tests-only PRs skip it entirely.

**ADVISORY GATE since Wave 5** (was informational at Wave 3). The check reports
`conclusion=failure` when a baselined metric sits >2pp below baseline, when
refusal precision is below 90% absolute, or when the eval cannot produce a
usable number (fail closed). The verdict lives in
`velnor-evals/harnesses/gate_assertion.py`, not in the workflow.

**It does not block merge.** GitHub Free does not enforce required status checks
on private repos and `DEC-0062` defers branch protection to V1.5 — see
[`branch-protection/V1_5-ACTIVATION.md`](./branch-protection/V1_5-ACTIVATION.md)
for the one-step activation and the exact required-check context string.

```yaml
permissions:
  contents: read
  pull-requests: read    # only if you want the 2-key override recorded

jobs:
  eval-on-pr:
    uses: VelnorLabs/.github/.github/workflows/_eval-on-pr.yml@main
    # callers OUTSIDE velnor-evals must also pass a read token for the
    # private velnor-evals repo (github.token cannot read other repos):
    # secrets:
    #   evals-checkout-token: ${{ secrets.VELNOR_EVALS_READ_TOKEN }}
```

The called workflow declares **no job-level `permissions:` block**, on purpose.
A job-level block replaces inheritance, and a called workflow requesting a scope
its caller did not grant fails the entire run at startup — so declaring
`pull-requests: read` there would hard-break every caller granting only
`contents: read`. Each caller sets its own least-privilege scopes instead.

### `unreachable-evals-is-failure` (default `true`)

A caller that cannot fetch velnor-evals now goes **red** instead of
green-with-a-warning. This matters: before T-W5-003, velnor-iac's `eval/on-pr`
check reported **pass on every PR while having skipped the eval entirely** — its
`VELNOR_EVALS_READ_TOKEN` does not resolve, so the workflow degraded to
"available=false" and exited 0. A check that has never evaluated anything is the
LE-374 shape: indistinguishable from a working one.

To fix a red caller, repair the token. To opt out deliberately, set
`unreachable-evals-is-failure: false` — the job then says loudly in its summary
that it is reporting success without having evaluated anything.

### 2-key override

`eval-override` label + **two distinct staff approvals on the current head
commit** + a `## Eval override rationale` section in the PR body. Staff status
is resolved live from the collaborator API (no committed staff list). A valid
override is **recorded, not honoured** — the check stays red, because under the
descope there was never a blocked merge for it to unblock. See
`velnor-evals/harnesses/gate_override.py`.

## SDK bypass check (`_sdk-bypass-check.yml` · T-W0-020)

A second reusable workflow that **blocks a PR if service code imports AWS, a
database driver, or an AI provider SDK directly** instead of going through the
platform SDK (`velnor_platform_sdk` for Python, `velnor_platform_sdk_go` for
Go). It is what keeps tenant isolation, cost tripwires, audit logging, PII
tokenization, and OTEL tracing from being silently bypassed by a stray
`import boto3`.

> **Note on naming:** there is no `BYPASS_LINT_BLOCKS_SENTINEL` symbol in the
> codebase. The "bypass check" is the `import_audit` tool below; the "sentinel"
> is its self-test suite (the `test_*_import_fails` cases) that proves the tool
> actually fails on a forbidden import. If you came here from that ledger name,
> this section is what it meant.

### Wiring it into a service repo

Add a job to the repo's `.github/workflows/ci.yml`:

```yaml
  sdk-bypass-check:
    uses: VelnorLabs/.github/.github/workflows/_sdk-bypass-check.yml@main
    with:
      repo-type: py        # or: go
    secrets: inherit       # passes VELNOR_SDK_READ_TOKEN through
```

The workflow checks out the calling repo, fetches the matching `import_audit`
tool from the private SDK repo (using `VELNOR_SDK_READ_TOKEN`), runs it, posts
a PR comment listing violations on failure, and **exits non-zero to block the
PR** if any unexempted violation remains. `scan-root` (default `.`) narrows the
scan; `sdk-py-ref` / `sdk-go-ref` (default `main`) pin which SDK ref the tool
is pulled from.

### What it enforces

The audit walks every source file and flags imports matching a forbidden list:

| | Forbidden import substrings |
|---|---|
| **Python** (`FORBIDDEN_PREFIXES`) | `boto3`, `botocore`, `psycopg`, `psycopg2`, `asyncpg`, `anthropic`, `openai`, `appconfigdata` |
| **Go** (`forbiddenSubstrings`) | `aws/aws-sdk-go-v2`, `aws/aws-sdk-go/` (v1), `lib/pq`, `jackc/pgx`, `anthropic-sdk-go`, `openai-go` |

Files inside the SDK package tree itself are exempt (path segments
`velnor_platform_sdk` / `velnor_platform_sdk_go` and `tools/`) — the SDK *is*
the layer that's allowed to import these directly.

**Exit codes:** `0` = clean, `1` = violations found (PR blocked), `2` = tool
error (bad root, unreadable allowlist).

### Per-repo exemptions: `.import-audit-allow`

When the SDK doesn't yet provide a facade for some capability, a repo can
exempt a specific (file, import) pair by adding a `.import-audit-allow` file at
its scan root. One exemption per line, `#` comments allowed:

```
# <file-path-substring> <import-path-substring>
internal/services/tenant_provisioner jackc/pgx
```

A violation is exempted only when its file path contains the first substring
**and** its import path contains the second — so the exemption is scoped to one
call site, not a blanket allow. Exemptions are intentionally narrow and should
carry a comment explaining why (and a note to remove them once an SDK facade
lands). See `velnor-admin-api/.import-audit-allow` for a live example (admin
schema DDL that sits outside the per-tenant `tenancy.WithTenant` RLS model).

### How the sentinel proves the gate works

The audit tool ships with its own test suite (`tests/test_import_audit.py` in
sdk-py, `tools/import_audit/main_test.go` in sdk-go). These are the "sentinel"
tests: each plants a known-forbidden import in a temp tree and asserts
`audit(...) == 1`, plus mirror cases asserting SDK-internal files return `0`.
If someone weakens the forbidden list or the exemption logic, these tests fail
in the SDK repo's own CI — so the gate can't be quietly defanged. A companion
cross-language contract test keeps the Python and Go forbidden lists from
drifting apart.

### `_schema-bypass-check.yml` (T-W1-018)

A sibling workflow with the same shape. It clones `velnor-schemas@main` to get
the canonical exported-type name list, then scans the calling repo for
**top-level type definitions that shadow a canonical schema name** (TypeScript,
Python, or Go) and fails if any shadow is found — so services consume the
generated schema types instead of redeclaring them. Wire it the same way:

```yaml
  schema-check:
    uses: VelnorLabs/.github/.github/workflows/_schema-bypass-check.yml@main
    secrets: inherit
```

`scan_paths` (default `src`) narrows the scan.

## GitHub OIDC

The OIDC AWS provider + `velnor-github-actions` IAM role live in
`velnor-iac/accounts/dev/github_oidc.tf`. The role ARN is stored as an
org-level Actions variable `VELNOR_GHA_ROLE_ARN` so all repos can reference
it via `${{ vars.VELNOR_GHA_ROLE_ARN }}`.

## Branch protection ruleset

The `branch-protection/ruleset.tf` must be applied once by a human org-admin:

```bash
cd branch-protection
TF_VAR_github_token=<org-admin-pat> tofu init && tofu apply
```

Rules enforced on every `VelnorLabs/*` repo's default branch:
- 1 required approving review + code-owner review
- Signed commits (GPG or SSH) required
- CI status checks must pass (lint + unit-tests + build)
- No force-push
- No branch deletion

> **It has never been applied, and on the current plan it cannot be.** GitHub
> Free does not enforce branch protection or rulesets on private repos, and
> org-level rulesets need Team+. `DEC-0062` defers this to V1.5. See
> [`branch-protection/V1_5-ACTIVATION.md`](./branch-protection/V1_5-ACTIVATION.md)
> for the prerequisites, the eval-gate required check (authored, commented out),
> and a warning that the three contexts listed above (`lint`, `unit-tests`,
> `build`) match no check any repo currently reports — applying as-is would
> block every PR in the org on contexts that never report.

## References

- T-W0-010 — this task
- T-W0-011 — shared CI workflow (successor)
- DEC-0056 — org is VelnorLabs
- DEC-0057 — single dev account (velnor-dev, us-east-1)
- velnor-iac/accounts/dev/github_oidc.tf — OIDC TF
