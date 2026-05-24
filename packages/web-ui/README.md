# fulcra-collect web UI

Browser-based UI for Fulcra Collect. The daemon's HTTP server (in
packages/collect/fulcra_collect/web.py) serves these static files
from `dist/`.

Tech: vanilla HTML5 + CSS3 + JavaScript + Alpine.js (CDN) +
Tailwind CSS (CDN). No build step.

## Local development

The daemon serves this dir automatically when started:

```
fulcra-collect daemon
```

The web URL is printed in the daemon's logs and written to
`~/.config/fulcra-collect/web-url`. Open it in your browser.

To live-edit: just save files in `dist/`. Reload the browser.

## Architecture pointers

- Frontend talks to the daemon via JSON at `/api/*` with a Bearer
  token (the daemon sets a `fulcra_token` cookie on the initial
  HTML load).
- The wizard renderer (in `wizard.js`, Phase C) walks each plugin's
  `setup_steps` array fetched from `/api/plugin/{id}/contract`.
- All HTTP routes documented in
  `docs/superpowers/specs/2026-05-24-fulcra-collect-web-ui-design.md`.
