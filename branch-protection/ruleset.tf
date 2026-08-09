# ---------------------------------------------------------------------------
# T-W0-010 · VelnorLabs GitHub org branch-protection ruleset
#
# Requires: GitHub provider + org admin token.
# Apply via: TF_VAR_github_token=<org-admin-token> tofu apply
#
# This file is authoritative for the org ruleset. It is NOT applied
# automatically by velnor-github-actions (that role has no org admin scope).
# A human org-admin runs this once, then it self-manages.
#
# Ruleset applies to ALL repos in VelnorLabs on creation.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    github = {
      source  = "integrations/github"
      version = "~> 6.3"
    }
  }
}

variable "github_token" {
  description = "GitHub org-admin PAT (classic) with admin:org + repo scopes"
  type        = string
  sensitive   = true
}

provider "github" {
  token = var.github_token
  owner = "VelnorLabs"
}

# ---------------------------------------------------------------------------
# Org-level branch protection ruleset: applies to every repo's default branch
# ---------------------------------------------------------------------------

resource "github_organization_ruleset" "main_branch_protection" {
  name        = "velnor-main-branch-protection"
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
    # Apply to all repos in the org
    repository_name {
      include = ["~ALL"]
      exclude = []
    }
  }

  rules {
    # Require pull request before merging
    pull_request {
      required_approving_review_count   = 1
      dismiss_stale_reviews_on_push     = true
      require_code_owner_review         = true
      require_last_push_approval        = true
      required_review_thread_resolution = true
    }

    # Require status checks to pass (CI workflow)
    required_status_checks {
      strict_required_status_checks_policy = true

      required_check {
        context        = "lint"
        integration_id = 0
      }
      required_check {
        context        = "unit-tests"
        integration_id = 0
      }
      required_check {
        context        = "build"
        integration_id = 0
      }

      # -----------------------------------------------------------------------
      # T-W5-003 · eval gate · READY TO APPLY, DELIBERATELY NOT APPLIED
      #
      # Uncommenting the block below is the ONE STEP that promotes the advisory
      # eval gate to a merge-blocking one. Do it as part of the DEC-0062 V1.5
      # plan upgrade — not before, because it cannot work before:
      #
      #   - GitHub Free does not enforce branch protection or rulesets on
      #     PRIVATE repos. Verified 2026-08-09, not inferred:
      #       gh api repos/VelnorLabs/velnor-evals/branches/main/protection
      #         -> 403 "Upgrade to GitHub Pro or make this repository public"
      #       gh api repos/VelnorLabs/velnor-evals/rulesets  -> 403 (same)
      #       gh api orgs/VelnorLabs -> plan.name="free", owned_private_repos=19
      #     The token holds admin:org + repo, so this is a PLAN limit, not a
      #     permissions gap. Org-level rulesets additionally require Team+.
      #   - DEC-0062 (OPERATIVE, 2026-06-14) chose to defer branch protection to
      #     V1.5 rather than upgrade, with the stated consequence "a red CI does
      #     not block merge by rule" and "treat green CI as a convention, not a
      #     gate, until V1.5". A FOUNDER RULING on 2026-08-09 held that deferral
      #     when Wave-5 hit its forcing function.
      #
      # THE CONTEXT STRING IS THE PART THAT IS EASY TO GET WRONG. It is
      # "eval-on-pr / eval/on-pr", NOT "eval/on-pr". For a reusable workflow,
      # the check name GitHub reports is "<caller job id> / <called job name>":
      # the caller's job is `eval-on-pr` and _eval-on-pr.yml names its job
      # `eval/on-pr`. Read off live runs (velnor-evals PRs #65/#66/#67), not
      # guessed. A required check whose context never reports can never be
      # satisfied — that is LE-374, where this repo's own required `ci` context
      # matched no job and every PR could only land via --admin.
      #
      # Before uncommenting, re-verify the string on a current PR:
      #   gh pr checks <PR> --repo VelnorLabs/velnor-evals
      #
      # See branch-protection/V1_5-ACTIVATION.md for the full checklist.
      # -----------------------------------------------------------------------
      # required_check {
      #   context        = "eval-on-pr / eval/on-pr"
      #   integration_id = 0
      # }
    }

    # Require signed commits (Invariant §3)
    required_signatures = true

    # No force-push to main
    non_fast_forward = true

    # No deletion of protected branch
    deletion = true
  }
}

# ---------------------------------------------------------------------------
# Output: ruleset ID for reference
# ---------------------------------------------------------------------------

output "branch_protection_ruleset_id" {
  description = "ID of the org-level branch protection ruleset"
  value       = github_organization_ruleset.main_branch_protection.id
}
