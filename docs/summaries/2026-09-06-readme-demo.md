# Session: README and mobile QA demo

**Branch:** codex/readme-demo
**Date:** 2026-09-06

## Prompt

> Can you make the edits and create a demo video or pictures testing ios and android apps? Submit changes as a PR

## Work

Used an isolated worktree. Reorganized the README, aligned the quickstart, shortened
the sample report into a labeled excerpt, and corrected data-handling statements
after inspecting worker and publication code.

Selected original iOS and Android screenshots from recorded run
`20260906-005526-69f052`. Checked the images visually and checked the corresponding
action timestamps, source report, and backend count evidence. Included four PNGs
and selected metadata without raw transcripts or account identifiers.

## Decisions

Used pictures, one of the requested formats. No generated app screens, new mobile
QA run, or video rendering. Kept performance measurements separate from functional
test evidence. Left the main checkout and app code unchanged.

## Follow-up

> can include a section with instructions for agents so that someone can just point their agent to it and it sets everything up for them?

Added an agent setup guide covering repository discovery, prerequisite installation,
profile-aware app configuration, dedicated devices, skill integration, first-run
verification, and permission boundaries. Added a copyable README prompt. Addressed
review feedback about issue reuse and the sample exporter's manual privacy checks.
The follow-up passed all 80 tests and 63 relative-link/anchor checks. README is
120 lines after adding the setup prompt and linking the existing skill-install steps.

## Verification

- All 80 tests passed. The first sandboxed run could not execute `ps` in two
  process-liveness tests; rerunning with process inspection permitted passed.
- Checked 49 relative links and heading anchors across the changed guides.
- Verified all four PNG hashes and capture timestamps against the source archive,
  plus candidate/base SHAs, gate verdict, and summarized backend counts.
- Rendered README at desktop and phone widths, the demo/base comparison, and the
  workflow diagram. Images loaded and there was no page-width overflow.
- Documentation scan found no stale or dead references. README is 118 lines.
