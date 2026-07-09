---
name: fulcra-prefs
description: "Read, capture, and apply the user's cross-platform preferences stored in Fulcra. Routes by agent capability: CLI (preferred), raw HTTP, or MCP read-only."
homepage: "https://github.com/ashfulcra/fulcra-tools/tree/main/packages/fulcra-prefs"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "⚙️" } }
---

# fulcra-prefs

The user's preferences and facts live in their Fulcra account as typed,
decaying signals, compiled into per-platform preference documents. Your job:
LOAD them at session start, APPLY them, and CAPTURE new ones.

## Where to start — the re-entrancy probes

Before loading anything, probe how far this user already got. Enter at the
**first row whose probe fails**:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Authed? | `fulcra user-info` | exits 0 and prints valid JSON | [Pick your path](#pick-your-path) — install, then `fulcra auth login` |
| Onboarded? | `fulcra-prefs compile` | exits 0 (`compiled N keys …`); exit 2 with `not onboarded` means no definition / `prefs/meta.json` yet | [Onboarding a new user](#onboarding-a-new-user) — `fulcra-prefs onboard` |
| Prefs present? | `fulcra-prefs inject --platform <your-platform>` | non-empty output (a rendered preference block) | [Pick your path](#pick-your-path) tier-1 capture — nothing to load yet; start capturing |
| Hooks installed? | `grep -q fulcra-prefs-hooks ~/.claude/settings.json` (or `~/.codex/hooks.json`) | exits 0 — a managed SessionStart/capture hook is wired | [Onboarding a new user](#onboarding-a-new-user) — `fulcra-prefs install-hooks --platform <claude-code\|codex>` |

First failure wins. Hooks are optional: a CLI-capable agent runs `inject`/`capture`
by hand without them, so a hookless-but-onboarded user is fully usable — the last
row only tells you whether load/capture is automatic. All four pass → `inject` at
session start already carries their prefs; apply them and capture new ones.

## Pick your path

1. **You can run shell commands** → use the CLI. Setup once:
   `uv tool install fulcra-prefs` (and `fulcra auth login` if not authed).
   - Load: `fulcra-prefs inject --platform <your-platform>` → prepend output
     to your working context. Empty output = no prefs yet; continue silently.
   - Capture: `fulcra-prefs capture --key <ns.key> --value '<json>'
     --strength <-1..1> --platform <your-platform>` (see
     references/fulcra-prefs-capture.md for when and what to capture).
   - Auto-capture: notice preferences passively and record them in one call at
     session end — `fulcra-prefs capture-batch --file <json-array> --platform
     <your-platform>`; mark inferred items lower `confidence` (compile won't let
     a guess override an explicit pref). See the capture reference.
   - Text extraction: when you have raw user text but not a hand-built key/value,
     run `fulcra-prefs extract-candidates --platform <your-platform> --session
     <session_id> --text '<user text>' --write`. It only queues explicit
     preference language; lifecycle hooks still perform the capture.
   - Hooked auto-capture: if `fulcra-prefs install-hooks --platform
     <your-platform>` is installed, write candidates during the session to
     `~/.local/state/fulcra-prefs/candidates/<your-platform>/<session_id>.json`;
     the lifecycle hook drains that file through `capture-batch`.
     Prefer the helper command:
     `fulcra-prefs notice --platform <your-platform> --session <session_id>
     --key <ns.key> --value '<json>' --strength <n>`.
   - Refresh: `fulcra-prefs compile` (run after captures; cheap).
2. **You can make HTTP requests but not run commands** → follow
   references/fulcra-prefs-tier2-http.md (device-flow auth + direct API).
3. **You only have the Fulcra MCP** → you can read user data the MCP exposes,
   but preference write/read of the compiled docs is not available via MCP
   today. Tell the user to run onboarding from a CLI-capable agent.

## Onboarding a new user

If `inject`/`get` report not-onboarded: run `fulcra-prefs onboard` (requires
`fulcra auth login` first — account auto-creates on first login). For a full
guided platform onboarding, hand off to the fulcra-onboarding skill:
https://github.com/fulcradynamics/agent-skills/blob/main/skills/fulcra-onboarding/SKILL.md

## Rules

- NEVER print or store the user's access token.
- Respect scopes: per-platform overrides beat global; negative weight =
  aversion (don't suggest what they dislike).
- Capture is consent-adjacent: only capture what the user said or confirmed —
  see the capture reference for the heuristics.
- Platform details for Claude, Claude Code, ChatGPT, Codex, OpenClaw, and
  Hermes are in `references/platforms.md`.
