# Production release review — November 23, 2026

**Decision: NO-GO for Production deployment.** This is a frozen source candidate, not a published release or approval. Development can continue without changing these pins. Production remains disabled. DNS remains deferred at the user's request. Evidence was collected late November 23 Chicago time (November 24 UTC).

## Repository versions

Propose `release/v1.0.0` independently in each repository for the first deployment release. Existing package versions and native `v*` tags remain unchanged. These proposed tags have NOT been created. The manifest stays `draft`, with unresolved release-lock hashes, artifacts, and production operations left null. Four service image pins have historical signed-build evidence matching live Development digests; signatures were not independently reverified in this audit. CI alone does not prove Development acceptance.

| Repository | Proposed tag | Frozen source | Source CI | Pipeline PR |
|---|---|---|---|---|
| velnor-iac | release/v1.0.0 | `d3b2fe0e041b7745f3f925dc5d74185712a8aed1` | [success](https://github.com/VelnorLabs/velnor-iac/actions/runs/44630274570) | [#482](https://github.com/VelnorLabs/velnor-iac/pull/482) (draft) |
| velnor-schemas | release/v1.0.0 | `cae5f75a2cbc537464dcbeb458cc4df959611ec2` | [success](https://github.com/VelnorLabs/velnor-schemas/actions/runs/45077466438) | [#252](https://github.com/VelnorLabs/velnor-schemas/pull/252) (draft) |
| velnor-platform-sdk-go | release/v1.0.0 | `a88b073f79dffa12539f1be5874ed1d4aaf9f553` | [success](https://github.com/VelnorLabs/velnor-platform-sdk-go/actions/runs/44693679220) | [#76](https://github.com/VelnorLabs/velnor-platform-sdk-go/pull/76) (draft) |
| velnor-platform-sdk-py | release/v1.0.0 | `3869d1167f75db5b89dbecb171b17ffd64f534ff` | [success](https://github.com/VelnorLabs/velnor-platform-sdk-py/actions/runs/44656333076) | [#77](https://github.com/VelnorLabs/velnor-platform-sdk-py/pull/77) (draft) |
| velnor-design-tokens | release/v1.0.0 | `39e7510193ff0e331b4cc7a08afc19a67a9a313f` | [success](https://github.com/VelnorLabs/velnor-design-tokens/actions/runs/41169618969) | [#41](https://github.com/VelnorLabs/velnor-design-tokens/pull/41) (draft) |
| velnor-plane-api | release/v1.0.0 | `9e1d2727aaf6ee65b2ad0f10c1962ccea1fa8adf` | [success](https://github.com/VelnorLabs/velnor-plane-api/actions/runs/45081420068) | [#288](https://github.com/VelnorLabs/velnor-plane-api/pull/288) (draft) |
| velnor-admin-api | release/v1.0.0 | `682d557c65a74aa1f1caab3aff99f3cfc2792f79` | [success](https://github.com/VelnorLabs/velnor-admin-api/actions/runs/42162835974) | [#85](https://github.com/VelnorLabs/velnor-admin-api/pull/85) (draft) |
| velnor-operator-api | release/v1.0.0 | `6e653966cb74cfa2dd8e761db7f0446e48a22b64` | [success](https://github.com/VelnorLabs/velnor-operator-api/actions/runs/45078033941) | [#117](https://github.com/VelnorLabs/velnor-operator-api/pull/117) (draft) |
| velnor-user-api | release/v1.0.0 | `2e06c21eabe3a20f6debea0316f5e721a5b2cfc8` | [success](https://github.com/VelnorLabs/velnor-user-api/actions/runs/42696348912) | [#93](https://github.com/VelnorLabs/velnor-user-api/pull/93) (draft) |
| velnor-action-gate | release/v1.0.0 | `1787bd119016f3bf4a450ddcfb2b4bcf34719a85` | [success](https://github.com/VelnorLabs/velnor-action-gate/actions/runs/42712709948) | [#68](https://github.com/VelnorLabs/velnor-action-gate/pull/68) (draft) |
| velnor-workers | release/v1.0.0 | `492eff513b134fba980d89abf83cfa9fd5a3f46b` | [success](https://github.com/VelnorLabs/velnor-workers/actions/runs/42393563574) | [#82](https://github.com/VelnorLabs/velnor-workers/pull/82) (draft) |
| velnor-store-compliance-worker | release/v1.0.0 | `22fcd465add0dea9dff1584e50a19173dc384284` | [success](https://github.com/VelnorLabs/velnor-store-compliance-worker/actions/runs/41169607351) | [#25](https://github.com/VelnorLabs/velnor-store-compliance-worker/pull/25) (draft) |
| velnor-embed-worker | release/v1.0.0 | `20ea3b4baf4c3f01de166adccff82f75c3659fce` | [success](https://github.com/VelnorLabs/velnor-embed-worker/actions/runs/41860255397) | [#37](https://github.com/VelnorLabs/velnor-embed-worker/pull/37) (draft) |
| velnor-agent-gateway | release/v1.0.0 | `bc69c6f1dcd94c719ac46abdb8c84dbbef67ac0e` | [success](https://github.com/VelnorLabs/velnor-agent-gateway/actions/runs/44690449186) | [#84](https://github.com/VelnorLabs/velnor-agent-gateway/pull/84) (draft) |
| velnor-chat-api | release/v1.0.0 | `831cef30714aa5a039294532b4b5375c97c95384` | [success](https://github.com/VelnorLabs/velnor-chat-api/actions/runs/45315955704) | [#232](https://github.com/VelnorLabs/velnor-chat-api/pull/232) (draft) |
| velnor-web | release/v1.0.0 | `5455ddabae3921cd56a8c468dc0c0d6e0a35eae9` | [success](https://github.com/VelnorLabs/velnor-web/actions/runs/44978656715) | [#229](https://github.com/VelnorLabs/velnor-web/pull/229) (draft) |
| velnor-admin-web | release/v1.0.0 | `8b37b3d60e28bdb84dd3e952c5801daed6e4fa20` | [success](https://github.com/VelnorLabs/velnor-admin-web/actions/runs/42260644389) | [#51](https://github.com/VelnorLabs/velnor-admin-web/pull/51) (draft) |
| velnor-evals | release/v1.0.0 | `c53b87826f5bcec08e856c124edc4d74b5555284` | [success](https://github.com/VelnorLabs/velnor-evals/actions/runs/41169590969) | [#105](https://github.com/VelnorLabs/velnor-evals/pull/105) (draft) |
| velnor-mobile | release/v1.0.0 | `ad0b99e48c29917bf0eca6932ce54447857aefb7` | [queued](https://github.com/VelnorLabs/velnor-mobile/actions/runs/45316944634) | [#99](https://github.com/VelnorLabs/velnor-mobile/pull/99) (draft) |

## Pipeline readiness

All 19 repository pipeline PRs and central `.github` PR #61 are open drafts. They are not installed on main. The central branch is behind main; refresh and recheck before merging. Local production/release test suites passed 101 tests during this audit.

All 19 repositories report immutable releases disabled. The central `production` environment query returned HTTP 404 (absent or inaccessible); its Velnor-ai reviewer gate is not verified. No Production variables or repository secret names were returned. Enable immutable releases and configure the governance read token, reviewer gate, bypass restrictions and deployment controls before publishing releases.

The admin-web pipeline PR has a failed visual regression check: `admin-pulse-dashboard--fleet.png` differs by 0.0602% of pixels. Review the difference before accepting a new baseline. Mobile's exact source CI is still queued at the final poll. Evals main CI is green, but recent live-fleet nightly runs failed and require diagnosis. Older pipeline PR checks do not replace checks after integration with current main.

## Production prerequisites

1. Supply the Production account ID and authorized AWS profile/role. User confirmed these are not yet available. Do not use Development account 113831183566 as Production. Verify account security and CloudTrail from live evidence once access exists.
2. Bootstrap isolated state, OIDC roles, immutable artifact storage, KMS, deployment runners/CodeBuild projects and least-privilege access in us-east-1. Fill `production/targets/production.json` with verified values; produce and review the infrastructure plan before apply.
3. Resolve missing artifact/source provenance and Development acceptance across the full inventory, including web bundles, Lambda ZIP hashes/published versions, infrastructure/dependency sources and mobile delivery scope. Review source-to-artifact compatibility: current chat image includes schemas 0.153.0, while the schema source candidate reports 0.158.0; do not silently upgrade dependencies.
4. Close the runtime coverage gap: MLflow and ADOT/telemetry run in Development but are outside the original 19-repository manifest. Include them with source/artifact pins or explicitly exclude them from the first Production scope. Replace application-managed `latest` references with verified digests; document AWS-managed sidecars separately.
5. Implement and validate release-specific migration/check tasks, onboarding verification, secrets, restore/rollback procedures, monitoring and alert delivery. A backup reported by the user does not establish a tested restore or a Production rollback target.
6. Complete DNS/certificate configuration before customer traffic and end-to-end onboarding validation. DNS can remain deferred while preparing pipelines.

## Promotion and final approval sequence

1. Review this exact source freeze. Any source change creates a new candidate and an explicit delta; never silently refresh it from main.
2. Refresh and review pipeline PRs against current main; resolve failing checks. Merge reviewed pipeline code and configure repository governance. Keep Production deployment disabled during setup.
3. For each repository, prepare a release-lock PR binding the proposed version to this source SHA, exact CI, immutable artifact and Development acceptance evidence. Obtain independent review of the final PR head and merge it. Publish immutable `release/v1.0.0` only when the release gate passes.
4. Fill the platform manifest with the published release-lock hashes, production artifact references and concrete operations. Promote the tested artifacts without rebuilding; verify digests after cross-account copying. Re-run compatibility, migration and onboarding checks in Development because there is no staging environment.
5. Present the exact manifest checksum, infrastructure plan checksum, release delta, rollback plan and remaining findings to the user for **final approval**. The designated human reviewer is Velnor-ai. No approval is inferred from this review package or previous general authorization.
6. Only after readiness passes and final approval: activate and dispatch the gated Production workflow, verify health/onboarding, then record the deployed manifest and receipts as the baseline for future deltas.

There is no verified prior Production deployment manifest, so this first release has no established Production delta baseline. Future releases compare against the last successful deployed manifest, not against moving main branches.

## Evidence

`source-freeze.json` records exact sources, CI, pipeline checks, native version observations and signed-build evidence. `development-runtime.json` records sanitized ECS image digests and Lambda code hashes observed in Development. `SHA256SUMS` binds these files and the draft platform manifest. Native versions read from source are not proof of package publication.
