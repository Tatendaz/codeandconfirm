# Feature: GitHub Pages project page

**Branch:** feat/project-page
**Date:** 2026-10-20

## Summary

Add a static project page at `docs/index.html`, served by GitHub Pages from
`main:/docs` at https://tatendaz.github.io/codeandconfirm/. It follows the layout
and colors of the other Tatendaz project pages (claude-usage, yapui).

## Changes

- `docs/index.html`: one-line description, the duplicate-record demo with the
  recorded iOS and Android screenshots, backend counts, and report excerpt, who it
  is for, the agent setup prompt and manual install commands copied from the README,
  how the pipeline and verdict gate work, status and limits, and a "Built by" footer.
  Light and dark themes follow the system setting, with a toggle saved per browser.
- `docs/index.md`: the same page as Markdown for agents and crawlers.
- `docs/404.html`: a not-found page with links back to the project.
- `docs/.nojekyll`: serve files as-is, so the existing Markdown docs are not run
  through Jekyll.
- `docs/img/`: WebP copies of the mascot image and the two candidate screenshots,
  resized for the page. Each screenshot links to its original PNG in `docs/media/`.
- `docs/social-preview.png`: 1200x630 Open Graph and Twitter card image.

## Notes

No runtime, CLI, or gate behavior changes. All numbers on the page come from
`README.md`, `docs/demo.md`, `docs/review-workflow.md`, `docs/performance.md`, and
`pyproject.toml`. The pipeline diagram reuses the existing light and dark SVG exports.
