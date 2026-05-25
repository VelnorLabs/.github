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

## References

- T-W0-010 — this task
- T-W0-011 — shared CI workflow (successor)
- DEC-0056 — org is VelnorLabs
- DEC-0057 — single dev account (velnor-dev, us-east-1)
- velnor-iac/accounts/dev/github_oidc.tf — OIDC TF
