# Diagrams

`review-pipeline.workflow.json` is the source of the pipeline diagram on the front page. It is an
[Archify](https://github.com/tt-a1i/archify) workflow specification; the checked-in outputs are:

- `review-pipeline.html` — the interactive viewer (pan, zoom, search, relationship tracing, light/dark). Download and open it.
- `review-pipeline-light.svg`, `review-pipeline-dark.svg` — fixed-palette exports the README picks between with `<picture>`.
- `review-pipeline.svg` — the auto-theme export (follows the system colour scheme) for embedding elsewhere.

To change the diagram, edit the JSON, then regenerate and re-check:

```
archify validate workflow docs/diagrams/review-pipeline.workflow.json --quality showcase
archify deliver  workflow docs/diagrams/review-pipeline.workflow.json docs/diagrams/review-pipeline.html --quality showcase
archify visual-check docs/diagrams/review-pipeline.html
```

Export the SVGs from the viewer's **Export → SVG** menu (once per theme), or capture the download in headless
Chrome. The `*.visual-check.*` sidecars are local evidence and stay out of the repository.
