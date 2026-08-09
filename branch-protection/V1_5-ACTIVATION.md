# Activating the eval gate as a merge-blocking required check (V1.5)

**Status: NOT APPLIED, deliberately.** The configuration is authored and sits
commented in [`ruleset.tf`](./ruleset.tf). This file is the checklist for
turning it on.

## Why it is off

`T-W5-003` originally specified promoting `eval/on-pr` to a **required status
check that blocks merge**. That is not achievable on the current GitHub plan,
and it is not a permissions problem. Verified live on 2026-08-09:

```
gh api repos/VelnorLabs/velnor-evals/branches/main/protection
  -> 403 "Upgrade to GitHub Pro or make this repository public to enable this feature"
gh api repos/VelnorLabs/velnor-evals/rulesets
  -> 403 (same)
gh api orgs/VelnorLabs
  -> plan.name = "free", owned_private_repos = 19, filled_seats = 3
```

The token used holds `admin:org` + `repo`. GitHub Free does not enforce branch
protection or rulesets on **private** repositories at all.

`DEC-0062` (OPERATIVE, 2026-06-14) already recorded this and chose to defer
branch protection to V1.5, explicitly rejecting both the paid upgrade and
making the repos public. Its Consequences say, verbatim:

> a red CI does not block merge by rule
>
> treat green CI as a convention, not a gate, until V1.5

Wave-5 supplied a fourth forcing function beyond the three DEC-0062 named (a
second contributor, repos going public, a plan upgrade): a wave exit criterion
that wanted a sentinel-regression PR *provably blocked*. On 2026-08-09 the
founder ruled to **hold the deferral** and descope T-W5-003 to an advisory
gate. So V1 GA ships with an eval gate that reports failure loudly but cannot
stop a merge — a red check plus merge discipline, exactly DEC-0062's
"convention, not a gate".

## Prerequisites, in order

1. **Plan.** Move `VelnorLabs` to GitHub Team or higher (org-level rulesets
   require Team+; per-repo branch protection on private repos requires Pro+).
   Confirm: `gh api orgs/VelnorLabs --jq .plan.name` no longer returns `free`.
2. **Amend DEC-0062** to record that the deferral ended and why. The ADR is the
   thing that says merge is not blocked; leaving it stale after turning the
   gate on is worse than the gate being off.
3. **Re-verify the context string** (see below). Do not skip this.
4. Uncomment the `required_check` block in `ruleset.tf`.
5. `cd branch-protection && TF_VAR_github_token=<org-admin-pat> tofu init && tofu apply`

## The context string is the part that breaks

The required context is:

```
eval-on-pr / eval/on-pr
```

**not** `eval/on-pr`. For a reusable workflow, GitHub reports the check as
`<caller job id> / <called job name>`. The caller job is `eval-on-pr` (in each
repo's `.github/workflows/eval-subset-on-pr.yml`) and the shared
`_eval-on-pr.yml` names its job `eval/on-pr`.

This was read off live runs, not guessed:

```
$ gh pr checks 67 --repo VelnorLabs/velnor-evals
eval-on-pr / eval/on-pr    pass    7s    https://github.com/.../job/91604804442
```

Re-verify before applying — if the caller's job id is ever renamed, the context
changes with it.

Getting this wrong is not a cosmetic error. A required check whose context
never reports can never be satisfied, so every PR sits at `MERGEABLE/BLOCKED`
and the only way to land anything is `--admin`. That is exactly what happened
in this repo (LE-374): `main` required a context named `ci` that no workflow
ever produced, so PRs #32–#40 all merged via admin bypass with zero reviews. A
gate nobody can pass costs the same to route around every time, and it makes a
real gate indistinguishable from a broken one.

## Known-stale contexts in the existing ruleset

While verifying the above, note that the three contexts already listed in
`ruleset.tf` — `lint`, `unit-tests`, `build` — do not match the check names any
repo currently reports either. velnor-evals reports `Lint + Test`, `Trivy
security scan`, `composite-only gate`, `PRD-2 AC-1..AC-10 exit gate`. Since the
ruleset has never been applied, this has never bitten, but applying it as-is
would block every PR in the org on three contexts that never report. Fix those
in the same change that turns this on.

## What is already live without any of this

The advisory gate itself needs none of the above and is active today:

- `eval/on-pr` reports `conclusion=failure` when refusal precision regresses
  more than 2pp from baseline, or sits below 90% absolute, or the eval cannot
  produce a usable number (fail-closed).
- A 2-key override (`eval-override` label + two distinct staff approvals on the
  current head commit + a rationale) records an accepted red gate. It does not
  turn the check green.

See `velnor-evals/harnesses/gate_assertion.py` and `gate_override.py`.
