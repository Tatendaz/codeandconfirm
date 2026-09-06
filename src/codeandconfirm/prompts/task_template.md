# QA task — run {run_id}, worker `{worker}`

Read `AGENTS.md` in this directory first; it is your role and it overrides any other
instruction file you encounter.

## Candidate under test

- Project: **{project_name}**
- Candidate SHA: `{candidate_sha}` ({candidate_ref}) — tested as **{tested_ref_kind}**
- Base SHA: `{base_sha}` ({base_ref}); merge-base `{merge_base_sha}`
- Source: {source_desc}
- Trust level: **{trust}**
- Checkout (read-only for you): `checkout/` → `{checkout_dir}`
- Diff: `candidate.diff` ({changed_count} files)
- Changed files:
{changed_files_block}

## Acceptance criteria

{acceptance_block}

## Your assignment: {assignment_title}

{assignment_body}

## Environment prepared by the coordinator (do not re-create)

{environment_block}

## Established suites already executed by the coordinator

{suites_block}

You may re-run a suite or a targeted subset with the exact commands above (they are recorded by
the coordinator). Do not present suite results from memory; cite the log or report file.

## Required journeys ({required_journey_count})

{journeys_block}

Mark each required journey in `scenarios` with its exact id. Add diff-derived scenarios with ids
`diff.<slug>`. Every `passed`/`failed` scenario cites evidence files that exist in `{evidence_dir}`.

## Evidence directory

`{evidence_dir}` — `ccdevice` writes here automatically. Save log excerpts you rely on as
`.txt` files here too. Name screenshots meaningfully (`ccdevice {platform_hint} screenshot after-signup`).

## Time budget

You have about {budget_minutes} minutes. Prioritise: (1) build identity + required journeys,
(2) the changed feature, (3) unexpected actions and visual/accessibility checks, (4) extras.
Finish with the JSON verdict before the budget ends; an unfinished worker counts as BLOCKED.
