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
