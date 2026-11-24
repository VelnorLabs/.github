# Production pipelines: prepare now, pin the release later

This replaces the legacy production entry point, which started an AppConfig
deployment before its intentionally unwired migration gate. Development CI and
deployment templates are unchanged. Nothing in this change activates production,
creates AWS resources, changes GitHub settings, or chooses application versions.

## Release policy

See [RELEASE-STRATEGY.md](RELEASE-STRATEGY.md) for branch ownership, Development
capture, immutable release candidates and production-versus-candidate deltas.
Production apply/promotion is held until November 10, 2026, 00:00 America/Chicago
by `policy.json`; the date alone does not approve or trigger deployment.

## Repository versions and human approval

Nineteen platform repositories now have prepared release-workflow callers. Each
uses reviewed release-lock PRs and immutable `release/vX.Y.Z` releases; native
package versions remain separate. The platform manifest pins those versions,
source SHAs, artifacts and lock checksums. Production checks published release
evidence and requires `Velnor-ai` approval for the current workflow run.
Activation settings and remaining operational gates are listed in
[RELEASE-STRATEGY.md](RELEASE-STRATEGY.md). These are prepared pipelines, not an
assertion that production infrastructure or release evidence is ready.

## Delivered

- `production-infrastructure.yml`: offline preview, saved infrastructure plan,
  and explicit apply of a reviewed plan checksum. Apply never recomputes a plan.
- `promote-to-production.yml`: offline preview, then migration-check → migrate →
  ordered backend deployments → Web/admin publication → onboarding verification.
- A draft release manifest and separate production target bindings. No default
  SHA, production account, role, project, task revision or artifact is guessed.
- Python adapters for OpenTofu, ECS rolling services, published Lambda alias
  promotion, versioned Web bundles, and isolated Fargate migration/verification
  tasks. CodeBuild executes these adapters from the exact controller commit.
- Exact-commit source CI checks, production-account verification, all-container
  image pins, image retention tags, rollback pointers, bounded waits, and
  versioned KMS-encrypted receipts bound to source/release/controller/target.

The source commits used by CodeBuild are the later-pinned application/IaC
commits. Its secondary `controller` source is this repository at `GITHUB_SHA`.
No per-service workflow changes are necessary for this controller. New product
development can continue independently until the release manifest is frozen.

## Run locally now

```sh
python3 scripts/production/pipeline.py preview
python3 -m pytest scripts/production -q
python3 scripts/check_workflows.py
actionlint -no-color -oneline
```

Preview accepts an explicitly draft manifest and lists unresolved bindings.
Execution rejects drafts, null pins, branch/tag references, mutable container
tags, absent stages, wrong account resources, and unversioned Web artifacts.

## Activation checklist (not performed by this PR)

1. Merge the reviewed controller change into `VelnorLabs/.github` main. These
   workflows use no third-party Actions, respecting this repository's
   `allowed_actions: local_only` policy. Run `preview` from Actions.
2. Bootstrap the production account, encrypted/versioned receipt bucket,
   controller OIDC role and stage-specific CodeBuild roles/projects through
   reviewed `velnor-iac` changes. Infrastructure creation is deliberately
   separate from application promotion. First bootstrap cannot use a CodeBuild
   project that does not yet exist.
3. Build a reviewed CodeBuild runner image with Python **3.12+**, AWS CLI v2,
   Git and the required OpenTofu binary. Use an immutable production ECR digest.
   Python 3.12 is needed for safe tar extraction. The OpenTofu version must match
   the later release operation. Commit provider lock files in each IaC workspace.
4. Configure one production project per `targets/production.json` binding:
   - name begins `velnor-prod-`; tag `Environment=prod` or `production`;
   - GitHub primary source is the component's repository;
   - its configured buildspec path equals the binding's sentinel path (for
     example `buildspec-production-backend.yml`); direct project dispatch is
     unsupported. The controller supplies the real, reviewed inline buildspec;
   - exactly one GitHub secondary source, identifier `controller`, URL
     `https://github.com/VelnorLabs/.github`; both sources are pinned per run;
   - custom Linux runner image equals `runner_image`, with the correct image
     pull permission; service role begins `velnor-prod-` in the target account;
   - unzipped S3 artifacts in the configured receipt bucket, encryption enabled,
     customer-managed KMS key configured and pinned as `receipt_kms_key_arn`; CloudWatch logs enabled;
   - access to private GitHub source via the configured CodeBuild connection;
   - VPC/private subnet connectivity where needed, scoped task-role passing,
     no development access, no webhook/automatic release trigger.
5. Configure the public controller repository's `production` environment:
   required reviewers, main-only branch selection, prevent self-review where
   staffing permits, and disallow bypass where supported. Setting
   `environment: production` in YAML does **not** create these protection rules.
   Required-reviewer availability depends on repository visibility and plan.
6. Set environment variables `PRODUCTION_ACCOUNT_ID`, `PRODUCTION_AWS_REGION`,
   `PRODUCTION_ROLE_ARN` (a `velnor-prod-*` role). Its OIDC trust must bind the
   actual subject for `VelnorLabs/.github` and the `production` environment,
   audience `sts.amazonaws.com`. Default subject:
   `repo:VelnorLabs/.github:environment:production`. Check for any customized
   subject before provisioning trust. No wildcard repository trust.
7. Set environment secret `PRODUCTION_SOURCE_READ_TOKEN` with read-only Actions
   and repository access to the source repositories. This verifies recorded CI
   runs; it is never passed to CodeBuild or included in the release manifest.
   GitHub's controller token cannot read other private repositories by default.
8. Build/test production-configured Web bundles from the chosen source after
   development settles. Include root `index.html` and `production-build.json`:
   `{"environment":"production","source_sha":"<40 hex>","config_sha256":"<64 hex>"}`.
   Hash the public runtime/build configuration, not secret values. Store the
   bundle as `.tar.gz` in versioned S3 and retain its version/checksum. This
   pipeline promotes verified bundles; it does not rebuild a development bundle
   with production secrets or assume development compiled configuration is safe.
9. Supply migration-check/apply and onboarding-verification task definitions
   implementing the evidence contract below. These application-specific task
   entrypoints still need wiring/testing in their source repositories; the
   controller never substitutes a successful no-op. Also supply real production
   infrastructure, target services/aliases, destination buckets/distributions
   and the migration set hash. Provision schedules/Step Functions through IaC.
10. Complete `targets/production.json` and a reviewed release JSON. Reconcile
    each artifact digest against its build provenance and successful CI run.
    Enable `PRODUCTION_PIPELINES_ENABLED=true` only after IAM, reviewer rules,
    target/project configuration and a rehearsal are verified. Keep it unset
    while development continues and application versions remain unpinned.

The controller role needs only production CodeBuild Start/BatchGet/List/Stop,
receipt S3 read/version reads, manifest writes limited to `production/manifests/`,
and the associated KMS encrypt/decrypt permissions (plus STS identity). It does not
need database access or general deployment permissions. Each CodeBuild role
gets only its stage's resource permissions, receipt write/read and logs. Apply
and migration roles must not be reused by the plan or verification projects.
The runner cannot make broad IAM grants safe; scope these policies in IaC.

## Pinning the release when development is ready

Copy `releases/candidate.json` to a release-specific file. Fill every component's
full source SHA, successful main-branch push/manual CI run ID and exact workflow
path. List retained ECR digest or S3 version/checksum artifacts (an empty list is
appropriate for source-only dependencies). Fill each operation and set `state`
to `ready` only after review. The CI gate verifies the selected workflow, SHA,
repository, main branch and success; release review must ensure that workflow
includes the required checks and that artifact provenance corresponds to it.

Operations are deliberately null in the draft. Their supported forms are:

| Stage | Operation fields |
| --- | --- |
| infra-plan / infra-apply | Identical `{tofu_version, workspaces: [{path: "accounts/prod/<area>", variables: {...}}]}`. No secrets in variables/manifest. Use production secret stores. |
| backend ECS | `{kind: "ecs-service", cluster: "<prod cluster ARN>", service: "velnor-...", task_definition: "<prod task ARN:revision>", images: {"<container>": "<prod ECR URI>@sha256:..."}}`; every sidecar must also be pinned. |
| backend Lambda | `{kind: "lambda-alias", function: "<prod function ARN>", alias: "production", version: "<published numeric version>", code_sha256: "<Lambda base64 CodeSha256>"}`; publish code via the reviewed infrastructure/artifact preparation process. |
| migration-check / migrate / verify | `{cluster, task_definition, images, container, subnets: [...], security_groups: [...]}`; migrations additionally require `migration_set_sha256`. Check/apply must bind the same migration set. |
| web | `{environment: "production", destination_bucket: "velnor-prod-...", distribution_id, destination_kms_key_arn, artifact: {bucket,key,version_id,sha256}, build_metadata: {environment,source_sha,config_sha256}}`. |

The backend list is sequential, and its order is explicit in the manifest.
Reconcile it with the release's real dependencies before freezing it. A service
with several Lambda aliases or independently deployed resources should get
separate bindings/operations sharing the component source SHA. The `workers`
binding is not an assertion that every worker is an ECS service.

### Infrastructure plans

To prepare infrastructure before application pinning, make a separate copy of
`candidate.json`, pin only the IaC component plus both infrastructure operations
and target/runner/receipt settings, and mark that copy `ready`. Infrastructure
execution checks only its required pins; application promotion still rejects
its unpinned services. Preview always lists unresolved application pins. This
lets infrastructure preparation proceed while feature development continues.

Dispatch `production-infrastructure.yml` with `infra-plan`. Review the encrypted
plan bundle addressed in its receipt, including every workspace's review text.
Dispatch `infra-apply` with that exact bundle SHA256 and the same release/targets.
The adapter verifies bytes and source/release/target metadata before applying
the saved binary plans. OpenTofu's state checks still apply; stale plans require
a fresh review. Never bypass them by rebuilding a plan in the apply job.

Plans containing resource deletion/replacement or changing an existing ECS service
task definition/Lambda alias version are rejected. Promote application pointers
only after migration gates; destructive infrastructure needs a separate reviewed
procedure.

New dependency layers may need separate plan/apply cycles: a downstream
workspace cannot plan against outputs of an upstream workspace not yet created.
Do not put secrets in GitHub summaries; plan files stay in KMS-encrypted S3 with
restricted access and an appropriate retention policy.

### Application promotion

Dispatch `promote-to-production.yml` with `release` and the reviewed release
file. All source CI and project bindings are checked before the first stage.
Infrastructure changes and releases share one GitHub concurrency group. An
outstanding production CodeBuild run from a cancelled controller blocks a new
run; inspect or stop it before retrying. Remote tasks must also be checked after
an interrupted migration/verification build; do not assume cancelling GitHub
stops a Fargate task. Automatic retries are disabled.

Each stage returns a versioned receipt. Missing/stale receipts, wrong source
commits, migration-check failures and CodeBuild failures stop subsequent stages.
An ECS deployment that stabilizes on its old revision is a failure, not success.
Web publication uploads assets before HTML, retains old assets, records existing
index versions, and waits for CDN invalidation. Web smoke/functional checks are
performed by the mandatory final verification task, not inferred from upload.

### Migration and verification task evidence contract

The pinned task definition owns the command and IAM role. The adapter does not
inject a shell command. It runs with no public IP and passes these variables:

- `VELNOR_RELEASE_SHA256`, `VELNOR_STAGE`, `VELNOR_MIGRATION_SET_SHA256`
- `VELNOR_EVIDENCE_BUCKET`, `VELNOR_EVIDENCE_KEY` (unique to the build)

The task must write a JSON object to that S3 location, then exit zero:
`{status: "success", release_sha256, stage, migration_set_sha256, ...evidence}`.
Migration checks must additionally report `destructive_changes: false` after
checking the actual pending migration set against the database ledger. Apply
must revalidate the pending set/baseline, apply idempotently and report the same
set hash. Include uncovered/unclassified tenants as failures; do not accept an
empty registry as proof that clean-database bootstrap was performed. Bootstrap
and golden-template setup belong in the reviewed migration task implementation.

The verification task must fail on provisioning/login/invitation/core workflow
or cross-tenant isolation failures, use controlled production identities, and
write non-sensitive evidence. Exit zero alone is insufficient: evidence must
match the unique release/stage. No tenant data or credentials in receipts/logs.

## Recovery and completion boundaries

- Rollback pointers are written before ECS/Lambda/Web mutation, in the receipt
  bucket under `adapter-records/<build-id>/rollback.json`.
- Failed stages stop forward progress. A partial multi-service release is
  possible; recovery is explicit and does not roll back a database automatically.
- Keep previous task definitions, published Lambda versions, Web object versions
  and container images. The adapter tags candidate images `deployed-<digest>`;
  production ECR lifecycle policies must preserve that prefix and older releases.
- A first deployment may have no prior application release. Bootstrap target
  services before promotion; the service adapter requires an existing active
  ECS rolling service. Stop onboarding/writes if first-launch recovery requires it.
- This PR does not provision production, build customer-native binaries, submit
  to app stores, or claim a live deployment rehearsal. Mobile signing/distribution
  remains a separate delivery pipeline before native customer onboarding.

References: [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[OIDC subjects](https://docs.github.com/en/actions/reference/security/oidc),
[CodeBuild StartBuild](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html),
[CodeBuild source variables](https://docs.aws.amazon.com/codebuild/latest/userguide/build-env-ref-env-vars.html).
