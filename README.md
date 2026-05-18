# fulcra-attention

Capture what takes your attention while browsing — every page you read, with title and time-on-page — into your own [Fulcra](https://fulcradynamics.com) account.

This repo is the **Python relay + CLI** that the Chrome extension talks to. The extension itself is built in Plan B.

## Quickstart

```bash
pipx install -e .
fulcra auth login
fulcra-attention bootstrap
fulcra-attention setup
```

See `docs/superpowers/specs/` for design docs and `docs/superpowers/plans/` for implementation plans (mirrored from FulcraMediaHelpers).
