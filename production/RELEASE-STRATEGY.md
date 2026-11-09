# Velnor branch, version-lock and promotion strategy

## Agreed working plan — November 7, 2026

Two workstreams proceed independently:

1. Current application development continues through its existing review, merge
   and Development deployment process. The user will notify this chat after it
   is merged and deployed; this pipeline work will not take over or preempt it.
2. Production CI/CD is prepared on `feat/production-pipelines-20261107`, reviewed
   in a draft PR, and remains disabled/unpinned until the release is ready.

The earliest production change window is **Tuesday, November 10, 2026,
America/Chicago**, after Monday November 9. This is a not-before boundary,
not an appointment or automatic deployment. Readiness and release approval still
determine the actual time. `policy.json` enforces that boundary for infrastructure
apply and application promotion; preview and infrastructure planning can run
earlier. Production bootstrap changes are also held to this boundary unless the
user explicitly authorizes an earlier exception. Preparing code is permitted now.

## Branch strategy

| Branch/ref | Purpose | Deployment behavior |
| --- | --- | --- |
| `main` in each application repository | Reviewed integration of completed work | Existing Development CI/CD remains in place; a merge is not proof of deployment success. |
| `feat/<topic>`, `fix/<topic>`, `chore/<topic>` | Short-lived changes from main, reviewed by PR | PR validation; no production deployment. |
| `feat/production-pipelines-20261107` in `.github` | Production delivery work isolated from application changes | Preview/tests only until merged and explicitly activated. |
| `release/<release-id>` in the controller repository, when needed | Short-lived PR assembling the manifest/delta/acceptance evidence | Merge into controller main after review; execution is manual from main. |
| Immutable release reference/tag | Human-readable reference to the reviewed manifest commit | Reference only; never resolve a moving tag or branch as the deployment artifact. |

Keep the existing main-to-Development integration model. A permanent `develop`
or `production` source branch would introduce divergence without identifying the
exact combination of repositories running together. Production state is a
manifest plus verified deployment receipts, not the current head of a branch.

Use PR review, required checks and main-branch protection where supported by
the repository's plan/settings. Do not assume these settings already exist in
every private repository. The shared public controller must enforce review and
production environment protection before activation.

## Version lock: one release, many independently pinned repositories

Use an identifier such as `prod-2026-11-10-rc1` (illustrative until ready).
Versioned manifest files are append-only by procedure: a change after approval
creates `rc2`, with a new checksum and fresh acceptance/approval. `candidate.json`
is the draft template and must never be treated as the deployed release record.

Each frozen manifest records:

- Component repository, full commit SHA and successful CI workflow/run ID.
- Backend container digests, package/artifact pins, ECS task definition revisions
  or published Lambda versions. Preserve signatures/provenance and old artifacts.
- Schema/template and migration-set versions, including golden/bootstrap state.
- Production Web bundle object versions/checksums and public configuration hash.
  These may differ from Development bundles because auth/API configuration is
  compiled in; build from the same source and validate the actual production bundle.
- IaC source/tool version, target configuration, feature flags and deployment
  order. Operational changes count even when application commits do not change.
- Development deployment receipts and acceptance evidence supporting selection.
  The controller commit and target hash are recorded by execution receipts.

After the user confirms Development is deployed, capture the **observed running
state**. Read ECS/Lambda/Web/database state and deployment receipts, reconcile
each item to its source/artifact, and resolve mixed revisions or drift. Do not
freeze the latest main head by assumption, and do not infer a platform-wide
atomic release from individual successful CI jobs.

Freeze that verified Development snapshot as RC1. Later merges can continue to
Development; they do not change RC1. Shared dependencies must be compatible with
every pinned consumer. Never promote only the newest dependency independently
of the approved combination.

## Promotion sequence

1. **Develop:** finish the current work, pass CI, merge, deploy to Development.
2. **Observe:** inventory what is actually running and record deployment/test
   results. The user will tell us when to begin this capture.
3. **Freeze:** create RC1 with exact pins and explicit feature scope. Build/test
   production-specific client artifacts and preserve backend artifact digests.
   Copy backend images into production ECR with their digests intact, including
   required signatures/attestations; verify the destination before promotion.
4. **Review the delta:** compare the candidate against the last verified
   production manifest. For the first launch, explicitly use an empty baseline
   and label it an initial deployment. Include PRs/commits, dependency changes,
   migration/data changes, configuration/flags, rollback implications and
   customer-visible acceptance criteria. Check ancestry: a rollback or diverged
   history is not an ordinary forward release.
5. **Rehearse and approve:** finish operational checks, validate the exact RC,
   review infrastructure plans and approve the release checksum. If Development
   has moved on, use isolated rehearsal or controlled pre-customer validation;
   do not roll the shared Development environment backwards just to match RC1.
6. **Deploy no earlier than Tuesday:** apply reviewed infrastructure, run migration
   checks/apply, deploy pinned services and clients, and run onboarding/isolation
   verification. Stop at the first failed gate.
7. **Record actual state:** read back running revisions/digests and schema versions.
   Retain stage receipts and publish a complete-success release record only when
   mandatory verification passes. Then open customer onboarding.

An approved manifest is *desired state*. Receipts and live readback establish
*deployed state*. If a release fails halfway, record the per-component mixed
state and recover explicitly; never label either the old or new manifest as a
complete description until it is reconciled.

## Three records make the delta visible

| Record | Meaning | When it changes |
| --- | --- | --- |
| Development deployed snapshot | Last observed Development revisions and test evidence | After a verified Development deployment/readback. |
| Frozen production candidate | The exact combination approved for promotion | Never edited after approval; replace with a new RC. |
| Production deployed snapshot | Last verified running production combination, with receipts | After verified deployment or recovery; mixed states are recorded explicitly. |

Show two comparisons in every release review:

- **Production → candidate:** what this production release will add/change/remove.
- **Candidate → current Development:** additional work intentionally excluded
  from this release and available for the next one.

Also compare live production against its last recorded snapshot to detect drift.
Repository comparisons cannot detect an out-of-band task change or edited flag.
Configuration, migrations, artifact changes with unchanged source, removals and
rollbacks must remain visible in the report.

The implemented offline delta tool compares manifest components, artifacts,
operations and stage order. It provides exact-SHA compare links and marks null
pins as unresolved, never unchanged:

```sh
# First launch: no invented production baseline.
python3 scripts/production/release_delta.py --initial \
  --candidate production/releases/candidate.json

# After recorded manifests exist (filenames below are examples).
python3 scripts/production/release_delta.py \
  --base /path/to/verified-production.json \
  --candidate /path/to/frozen-rc1.json
python3 scripts/production/release_delta.py \
  --base /path/to/frozen-rc1.json \
  --candidate /path/to/verified-development.json --format json
```

This tool does **not** collect live inventory, enumerate merged PRs, establish
Git ancestry, or automatically update a production ledger. Those are explicit
follow-up steps during release capture/activation; do not describe its output
as proof of the live environment. Environment-specific target configuration
must be reviewed separately from source deltas.

## Candidate fixes and production hotfixes

Before first launch, fix through the normal feature/fix → main → Development
route, verify the fix, and create RC2. Re-run affected checks plus cross-service
acceptance; approval of RC1 does not authorize RC2.

After launch, normally use the same route. If main contains unrelated changes
that must not enter an urgent production hotfix, an exception may branch from
the deployed component SHA and cherry-pick only the reviewed fix. That path
needs an explicit hotfix CI/acceptance and promotion policy change: the current
controller intentionally accepts successful main-branch CI only. Do not weaken
that check ad hoc during an incident. Forward-port any exception fix to main.

Rollback is a separately approved manifest pointing at retained compatible
artifacts, with a fresh delta/verification record. Database rollback is not
automatic; additive schema compatibility and preservation of customer writes
determine whether to roll back the application or fix forward.

## Work until the first release

| Period | Application work | Production preparation |
| --- | --- | --- |
| Now through Monday Nov 9 | Current work finishes and deploys to Development under its existing owner. | Review pipeline PR, finish IaC/IAM/project definitions, build runner, wire migration/onboarding evidence, prepare tests and inventory/delta process. No production apply. |
| When the user confirms Development deployment | Capture and verify the running snapshot. Later work can continue independently. | Freeze RC1 and prepare exact production artifacts, deltas and readiness evidence. If not ready by Monday, move the freeze later. |
| Tuesday Nov 10 or later | Development continues; new changes belong to the next RC/release. | Deploy only after readiness and explicit approval, record deployed state, then onboard. No calendar-driven automatic deployment. |

Branch workflow reference: [GitHub flow](https://docs.github.com/en/get-started/using-github/github-flow).
The manifest, environment-record and promotion rules above are the proposed
Velnor operating policy, not a claim that GitHub settings or live inventory
automation are already configured.

## Reviewed repository versions (Development → Production)

Every platform repository has a `repository-release.yml` caller, pinned to a
reviewed commit of the shared `_repository-release.yml` controller. It validates
release-lock PRs and manually publishes reviewed versions. It does not build or
deploy on tag creation. The existing main/Development workflows remain intact.

Repository deployment-snapshot tags use `release/vMAJOR.MINOR.PATCH`, independently
per repository. This deliberately avoids triggering native `v*` schema/Go package
publication or replacing existing npm/Python versions. Record those native versions
in `native_versions`; bump package manifests using their existing checks. Start each
repository's deployment sequence at an explicitly reviewed version; do not infer
that every component must be 1.0.0 or have the same version. Use major for breaking
contracts, minor for backward-compatible features, patch for compatible fixes.

1. Continue short `feat/*` and `fix/*` branches into `main`. Validate and deploy to
   Development using existing workflows. No staging branch or environment.
2. Select the exact successful Development SHA. Build once where configuration
   permits; retain image digests, S3 object versions and checksums. Frontend builds
   with production configuration need separate provenance and production-config
   validation; a Development bundle cannot simply be relabelled.
3. Create `release/<version>` from main. Add or replace only
   `.release/release-lock.json`, following the example in that repository. Include
   CI and actual Development deploy/verification runs, required successful job
   names, native package versions, smoke evidence, known limitations and artifacts.
   Source-only libraries/IaC may have empty artifact lists; runtime artifacts must
   be recorded before promotion. Evidence text is a reviewer attestation; the
   publisher verifies run identity/jobs, not live AWS state or application semantics.
4. Open a normal release PR. An independent repository writer must approve its
   final head. Approval of an older revision is insufficient. Merge to main only
   after required checks pass. A sole contributor cannot self-approve this PR:
   assign another authorized writer or an authorized release bot as PR author.
5. Enable repository release immutability and `REPOSITORY_RELEASES_ENABLED=true`
   after reviewing the pipeline. Dispatch `Repository release` on main with that
   merged PR number. The publisher validates approval, ancestry, exact main CI,
   non-skipped required jobs and Development attestation, then creates an immutable
   GitHub release pointing at the original validated SHA, not the metadata merge.
   Existing tags are never overwritten. No production deployment is triggered.
6. In the central platform manifest, select each repository `version`, `source_sha`,
   `release_lock_sha256`, CI reference and matching artifacts. Unchanged components
   retain their prior versions. Generate/review the production-to-candidate delta.
   A changed candidate is a new RC/manifest review, never a moved version tag.
7. After readiness, manually dispatch the central workflow on main. The protected
   `production` environment must require exactly `Velnor-ai` and disable admin
   bypass. The controller also checks the run's approval history before AWS access.
   Use a fresh dispatch after failures; reruns are blocked to avoid stale approval.
8. Observe staged execution receipts and final onboarding verification. Record
   actual deployed versions, time, approver, run IDs and manifest digest. A partial
   rollout is a mixed inventory until every required stage is verified.

## Activation and remaining readiness work

- Merge the central draft PR before enabling repository callers; review their pinned
  controller SHA and grant Actions access to the shared workflow if required.
- Merge each repository pipeline PR. Configure immutable GitHub releases before
  setting `REPOSITORY_RELEASES_ENABLED`. Configure the repository secret
  `RELEASE_GOVERNANCE_READ_TOKEN` with Administration: read for the immutability
  settings endpoint. It is separate from the contents-write GITHUB_TOKEN and must
  have no production permissions. The publisher checks the setting at runtime.
- Protect main with required CI, stale-review dismissal and no force pushes. Protect
  `release/v*` tags, restrict workflow changes, and require CODEOWNERS review. Private
  repository protection depends on the organization's GitHub plan; pipeline checks
  are not a substitute for repository governance or protection against a malicious
  administrator. Audit effective settings before the first release.
- Configure `.github` production required reviewer `Velnor-ai`, disable admin bypass,
  and restrict deployment branches to main. Self-review prevention can only be
  enabled if a different identity initiates the workflow; do not inadvertently
  deadlock the sole named approver. The job still requires an explicit approval.
- Give the production source-read token read access to release metadata, Actions,
  and environment approval history. Separate publisher contents-write permissions
  from production OIDC roles; publishers have no production cloud credentials.
- Complete production accounts/IAM, CodeBuild bindings, artifact retention/signature
  verification, backup/restore exercise, safe database migration rehearsal in
  Development, monitoring, alerts, rollback rehearsal and onboarding smoke evidence.
  Review cross-service and schema compatibility against the complete pinned set.
- With no staging, freeze the candidate briefly in Development for platform-level
  smoke/tenant-isolation checks. New main commits may continue afterwards but do
  not change the pinned candidate. Use feature flags, backward-compatible migrations,
  and a limited initial onboarding cohort with explicit stop/rollback criteria.
- Mobile is versioned in the inventory. App-store signing, review and distribution
  are a separate release gate; the central AWS workflow does not publish mobile apps.
  Agree whether mobile distribution is required for the initial onboarding release.
- Tuesday 2026-11-10 America/Chicago is an earliest boundary, not a schedule or
  readiness assertion. No versions, settings, releases or production resources were
  activated by preparing these PRs.

GitHub references: [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
[environment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[workflow approval history](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run).
