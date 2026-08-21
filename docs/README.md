# Documentation site

This directory contains the Fumadocs site for `runpod-doc-worker`.

```bash
npm ci
npm run dev
```

Run the same checks used by the Pages workflow before opening a pull request:

```bash
npm run lint
npm run typecheck
npm run build
npm run validate:links
```

The static export is written to `out/`. GitHub Pages deploys that directory
after changes reach `main` and the repository's Pages source is set to GitHub
Actions.
