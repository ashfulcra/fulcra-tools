# AGENTS.md — autoloaded by Aider / Cursor / Continue.dev / Claude Code / OpenHands

This repo's agent-facing skill will live at `skills/fulcra-attention/SKILL.md` (added in Plan B). For now: this is the Python backend for fulcra-attention. There is no standalone relay — browser-extension events are ingested by the fulcra-collect daemon's `POST /api/extension/attention` endpoint. The `fulcra-attention` CLI (see `pyproject.toml` entry points) only handles headless/multi-machine management of the Attention definition, tags, and local state (bootstrap / setup / status / defs / adopt / reset).
