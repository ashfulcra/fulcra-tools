# Hermes-on-Daytona Demo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tooling to give each invited guest their own isolated, ephemeral Hermes agent on Daytona that onboards them into their own Fulcra account, with one operator command per guest that returns a clickable "press play" link.

**Architecture:** A reusable Daytona **Snapshot** bakes uv + the Fulcra CLI + Hermes (configured for OpenRouter) + the Fulcra onboarding skill + a localhost-only dashboard fronted by `socat`. Operator scripts (`build_snapshot.py`, `spawn.py`, `teardown.py`) use the Daytona Python SDK to build the snapshot once and then spawn/destroy per-guest sandboxes (30-min idle auto-stop), each returning a Daytona preview URL. No Fulcra credentials live anywhere in our infra — guests authenticate via the Fulcra device-code browser flow.

**Tech Stack:** Python 3.12, `daytona` SDK, `python-dotenv`, `pytest`, `ruff`; Hermes Agent (Nous Research) on OpenRouter; Daytona declarative image builder + Snapshots; `socat` for port forwarding inside the sandbox.

**Key references (verified May 2026):**
- Spec: `docs/superpowers/specs/2026-05-28-hermes-daytona-demo-design.md`
- Daytona SDK: import root is `daytona` (NOT legacy `daytona_sdk`). Client `Daytona(DaytonaConfig(api_key=, api_url="https://app.daytona.io/api", target="us"))`. Image builder `Image.debian_slim("3.12").run_commands(...).env({...})`. Snapshot `daytona.snapshot.create(CreateSnapshotParams(name=, image=), on_logs=)`. Spawn `daytona.create(CreateSandboxFromSnapshotParams(snapshot=, env_vars=, auto_stop_interval=30, public=True), timeout=)`. Background process via `sandbox.process.create_session(name)` + `execute_session_command(name, SessionExecuteRequest(command=, run_async=True))`. Preview `sandbox.get_preview_link(port)` → `.url`, `.token`. Teardown `sandbox.delete()` / `.stop()`.
- Hermes: install `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-browser --skip-setup`. Config dir `~/.hermes/` (`.env`, `config.yaml`, `SOUL.md`, `skills/`). OpenRouter via `.env` `OPENROUTER_API_KEY=` + `config.yaml` `model: {provider: openrouter, default: <id>}`. Dashboard `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`. Skills are Claude-style `SKILL.md` dirs in `~/.hermes/skills/`; install via `hermes skills tap add <gh repo>` + `hermes skills install ...`, or drop files. Preload at session start with `hermes -s <skill>`. No auto-first-turn-then-stay-interactive flag (issue #20799).

**Demo-grade access decision:** sandboxes are created `public=True` so the preview URL is a clean clickable link for non-technical guests. Control is "only invitees get the link" + ephemerality (30-min idle, torn down after). Stronger gating (token header / front auth) is out of scope for this phase; noted as a future option.

**Testing approach:** Pure logic (env loading, config rendering, param building, CLI parsing) is unit-tested with pytest/TDD. Integration with live Daytona/Hermes cannot be unit-tested; those steps are **live-verified** with explicit commands + expected output, gated on `.env` creds. The Task 1 spike produces a findings doc that later tasks rely on.

---

## File Structure

```
fulcra-hermes-daytona/
  pyproject.toml                      # project + deps + scripts
  .env                                # GITIGNORED (already created: DAYTONA_API_KEY, OPENROUTER_API_KEY, OPENROUTER_MODEL)
  .gitignore                          # already created
  README.md                           # operator runbook (Task 8)
  src/fhd/
    __init__.py
    config.py                         # load+validate env (.env) -> Settings
    image.py                          # declarative Daytona Image definition
    snapshot_params.py                # pure builders for snapshot/sandbox params
    build_snapshot.py                 # CLI: build/register the snapshot
    spawn.py                          # CLI: spawn one guest sandbox -> print preview URL
    teardown.py                       # CLI: list/delete guest sandboxes
  assets/hermes/
    config.yaml                       # Hermes model/provider config (baked into image)
    SOUL.md                           # agent persona + onboarding directive
    AGENTS.md                         # context: how to start onboarding on first message
    start-dashboard.sh                # launches hermes dashboard on 127.0.0.1 + socat to 0.0.0.0:8080
  docs/superpowers/
    specs/2026-05-28-hermes-daytona-demo-design.md
    plans/2026-05-28-hermes-daytona-demo.md
    findings/2026-05-28-spike-findings.md   # produced by Task 1
  tests/
    test_config.py
    test_snapshot_params.py
    test_cli_args.py
```

---

## Task 1: Live spike — de-risk Daytona + Hermes end to end

**Goal:** Before building modules, validate every uncertain integration in a real throwaway sandbox, and write down the exact working commands. This task is research+verification; its deliverable is a findings doc, not production code.

**Files:**
- Create: `docs/superpowers/findings/2026-05-28-spike-findings.md`
- Scratch (do not commit): a throwaway `spike.py`

**Context for the implementer:** Creds are in `.env` at repo root (`DAYTONA_API_KEY`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`). Use a Python venv with `uv`. The Daytona/Hermes API facts in this plan's header are from May-2026 docs but MUST be confirmed against the installed SDK and live behavior. Tear down every sandbox you create.

- [ ] **Step 1: Set up venv and install the SDK**

Run:
```bash
cd /Users/Scanning/Developer/fulcra-hermes-daytona
uv venv && source .venv/bin/activate
uv pip install daytona python-dotenv
python -c "import daytona, inspect; print(daytona.__version__ if hasattr(daytona,'__version__') else 'no __version__'); from daytona import Daytona, DaytonaConfig, Image, CreateSnapshotParams, CreateSandboxFromSnapshotParams, SessionExecuteRequest; print('imports OK')"
```
Expected: `imports OK` (if any import name differs, record the correct name in findings).

- [ ] **Step 2: Confirm the API key works (read-only)**

In `spike.py`, init the client from `.env` and list snapshots/sandboxes:
```python
import os
from dotenv import load_dotenv
load_dotenv()
from daytona import Daytona, DaytonaConfig
d = Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"], api_url="https://app.daytona.io/api", target="us"))
print("client OK")
```
Run `python spike.py`. Expected: `client OK` with no auth error. Record in findings: the correct `api_url`/`target` that worked.

- [ ] **Step 3: Build a minimal snapshot (uv + fulcra-api) and confirm it reaches `Active`**

Add to `spike.py`:
```python
from daytona import Image, CreateSnapshotParams
image = (
    Image.debian_slim("3.12")
    .run_commands(
        "apt-get update && apt-get install -y curl ca-certificates socat",
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
    )
    .env({"PATH": "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"})
    .run_commands("uv tool install fulcra-api")
)
snap = d.snapshot.create(CreateSnapshotParams(name="fhd-spike", image=image), on_logs=lambda c: print(c, end=""))
print("\nstate:", snap.state)
```
Expected: build logs stream; final state `Active`. **Record:** the actual uv install path (is `fulcra-api` on PATH as `/root/.local/bin` or the sandbox user's home?), and whether `socat` is available.

- [ ] **Step 4: Spawn a sandbox and verify the CLI runs**

```python
from daytona import CreateSandboxFromSnapshotParams
sb = d.create(CreateSandboxFromSnapshotParams(snapshot="fhd-spike", env_vars={}, auto_stop_interval=30, public=True), timeout=120)
print(sb.process.exec("bash -lc 'uv tool run fulcra-api --help'").result[:500])
```
Expected: the Fulcra CLI help text (confirms `uv tool run fulcra-api` works in-sandbox). **Record** the exact shell invocation that put `fulcra-api`/`uv` on PATH (`bash -lc` vs direct).

- [ ] **Step 5: Confirm the Fulcra device-code auth flow surfaces a URL**

```python
r = sb.process.exec("bash -lc 'timeout 20 uv tool run fulcra-api auth login || true'")
print(r.result)
```
Expected: stdout contains an authorization URL + device code (the command will hang waiting; the `timeout 20` cuts it). **Record** the exact stdout shape so the onboarding skill/agent know what to relay. Note whether the command writes the URL to stdout promptly (needed so the agent can relay it while it blocks).

- [ ] **Step 6: Install + start Hermes in the sandbox; resolve dashboard exposure**

```python
print(sb.process.exec("bash -lc 'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-browser --skip-setup'").result[-800:])
# write OpenRouter creds + model into ~/.hermes/.env and config.yaml (see assets in later tasks)
# start dashboard on localhost, forward to 0.0.0.0:8080 with socat, both backgrounded:
sb.process.create_session("dash")
sb.process.execute_session_command("dash", SessionExecuteRequest(command="bash -lc 'hermes dashboard --host 127.0.0.1 --port 9119 --no-open'", run_async=True))
sb.process.create_session("proxy")
sb.process.execute_session_command("proxy", SessionExecuteRequest(command="socat TCP-LISTEN:8080,fork,reuseaddr TCP:127.0.0.1:9119", run_async=True))
prev = sb.get_preview_link(8080)
print("OPEN:", prev.url, "token:", prev.token)
```
Open `prev.url` in a browser. **Record in findings, decisively:**
  - Does the dashboard load over the preview URL via the socat forward? (If `public=True`, no token needed.)
  - Does binding `127.0.0.1` + socat avoid the Nous OAuth gate that `--host 0.0.0.0` triggers? If socat doesn't work, test `hermes dashboard --host 0.0.0.0 --port 8080 --no-open --insecure` and record whether `--insecure` bypasses the OAuth gate.
  - The exact, working dashboard+exposure command(s) — this becomes `start-dashboard.sh`.

- [ ] **Step 7: Resolve the onboarding auto-start mechanism**

In the dashboard Chat tab (or via `hermes -s` test), determine the most reliable way to make the agent begin the Fulcra onboarding skill. Test, in order, and record which works:
  1. Skill preloaded (`hermes -s fulcra-onboarding`) + a directive in `SOUL.md`/`AGENTS.md` so the agent runs onboarding on the guest's first message.
  2. Whether the dashboard chat can be configured to launch the underlying `hermes` with `-s fulcra-onboarding` (inspect `hermes dashboard --help` / config keys).
  3. Whether a seeded first message is possible.
**Record** the chosen mechanism + exact config/flags. Also confirm how to install the skill: `hermes skills tap add fulcradynamics/agent-skills` then `hermes skills install fulcradynamics/agent-skills/fulcra-onboarding` (record the exact working command, and the env-var-driven source for swappability).

- [ ] **Step 8: Manually walk the full guest flow once**

From the browser dashboard: send a first message, confirm the agent starts onboarding, runs `fulcra-api auth login`, relays the URL+code into chat, you complete device login in your browser with a throwaway Fulcra account, and `user-info` then succeeds. **Record** any rough edges (e.g., agent needs to run auth in background and poll). This is the acceptance bar the built artifacts must reproduce.

- [ ] **Step 9: Tear down and write findings**

```python
sb.delete()
# optionally d.snapshot delete fhd-spike if a delete method exists; otherwise note it
```
Write `docs/superpowers/findings/2026-05-28-spike-findings.md` capturing every "Record" above as concrete, copy-pasteable commands and decisions. Delete `spike.py` (do not commit it).

- [ ] **Step 10: Commit findings**

```bash
git add docs/superpowers/findings/2026-05-28-spike-findings.md
git commit -m "docs: spike findings for Hermes-on-Daytona (pinned working commands)"
```

**Done = ** findings doc committed with: confirmed SDK import/method names, working PATH/shell for fulcra-api, device-flow stdout shape, the exact dashboard-exposure command, and the chosen onboarding auto-start mechanism.

---

## Task 2: Project scaffold + config module (TDD)

**Files:**
- Create: `pyproject.toml`, `src/fhd/__init__.py`, `src/fhd/config.py`, `tests/test_config.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "fhd"
version = "0.1.0"
description = "Fulcra Hermes Daytona demo — operator tooling"
requires-python = ">=3.12"
dependencies = ["daytona", "python-dotenv"]

[project.optional-dependencies]
dev = ["pytest", "ruff"]

[project.scripts]
fhd-build = "fhd.build_snapshot:main"
fhd-spawn = "fhd.spawn:main"
fhd-teardown = "fhd.teardown:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
pythonpath = ["src"]
```

- [ ] **Step 2: Write the failing test for config loading**

`tests/test_config.py`:
```python
import pytest
from fhd.config import Settings, load_settings

def test_load_settings_reads_required_env(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-y")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
    s = load_settings()
    assert s.daytona_api_key == "dtn_x"
    assert s.openrouter_api_key == "sk-or-y"
    assert s.openrouter_model == "anthropic/claude-sonnet-4.5"
    assert s.daytona_api_url == "https://app.daytona.io/api"
    assert s.daytona_target == "us"

def test_load_settings_missing_required_raises(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError) as e:
        load_settings(use_dotenv=False)
    assert "DAYTONA_API_KEY" in str(e.value)

def test_openrouter_model_defaults(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dtn_x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-y")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    s = load_settings(use_dotenv=False)
    assert s.openrouter_model == "anthropic/claude-sonnet-4.5"
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `uv run --extra dev pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: fhd.config`).

- [ ] **Step 4: Implement `src/fhd/config.py`**

```python
"""Load and validate operator credentials/config from the environment.

WHY: every script needs the same creds (Daytona + OpenRouter). Centralizing
load+validation here means a missing key fails fast with a clear message
instead of an opaque SDK auth error three calls deep.
"""
from __future__ import annotations
import os
from dataclasses import dataclass

DEFAULT_API_URL = "https://app.daytona.io/api"
DEFAULT_TARGET = "us"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

@dataclass(frozen=True)
class Settings:
    daytona_api_key: str
    openrouter_api_key: str
    openrouter_model: str
    daytona_api_url: str
    daytona_target: str

def load_settings(use_dotenv: bool = True) -> Settings:
    if use_dotenv:
        from dotenv import load_dotenv
        load_dotenv()
    missing = [k for k in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY") if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing required env var(s): {', '.join(missing)} (set them in .env)")
    return Settings(
        daytona_api_key=os.environ["DAYTONA_API_KEY"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        daytona_api_url=os.environ.get("DAYTONA_API_URL", DEFAULT_API_URL),
        daytona_target=os.environ.get("DAYTONA_TARGET", DEFAULT_TARGET),
    )
```
Also create empty `src/fhd/__init__.py`.

- [ ] **Step 5: Run the test, verify it passes**

Run: `uv run --extra dev pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/fhd/__init__.py src/fhd/config.py tests/test_config.py
git commit -m "feat: project scaffold + env-backed Settings loader with validation"
```

---

## Task 3: Hermes asset files (config, persona, onboarding directive, dashboard launcher)

**Goal:** The static files baked into the image. Use the exact values confirmed in Task 1 findings; the content below is the starting point.

**Files:**
- Create: `assets/hermes/config.yaml`, `assets/hermes/SOUL.md`, `assets/hermes/AGENTS.md`, `assets/hermes/start-dashboard.sh`

- [ ] **Step 1: Write `assets/hermes/config.yaml`**

```yaml
# Hermes model config — OpenRouter. The API key is injected at runtime via
# ~/.hermes/.env (OPENROUTER_API_KEY); the model id is injected at runtime
# from OPENROUTER_MODEL (start-dashboard.sh writes it). Keep this file secret-free.
model:
  provider: openrouter
  default: anthropic/claude-sonnet-4.5   # overwritten at boot from $OPENROUTER_MODEL
skills:
  creation_nudge_interval: 0
```

- [ ] **Step 2: Write `assets/hermes/SOUL.md`**

```markdown
# Soul

You are a Fulcra demo agent. You are ephemeral — this sandbox is temporary —
but the user's memory is permanent in their own Fulcra account. Your first job
is always to get the user set up with Fulcra so their data persists beyond you.

On the user's first message, immediately begin the `fulcra-onboarding` skill.
Do not wait for them to ask. Greet them as that skill instructs, then guide
them through creating or logging into their own Fulcra account.
```

- [ ] **Step 3: Write `assets/hermes/AGENTS.md`**

```markdown
# Onboarding directive

When a session starts, your first action is to run the `fulcra-onboarding`
skill (read its SKILL.md and follow it). The skill greets the user; you do not
add a separate greeting.

To authenticate Fulcra you will run `uv tool run fulcra-api auth login`, which
prints an authorization URL and a device code and then waits while the user
logs in. Run it so you can read its stdout, then present the URL and code to
the user in chat and tell them to open the URL in their own browser to create
a new Fulcra account or sign in. Poll `uv tool run fulcra-api user-info` to
confirm success before continuing. Never ask the user for a Fulcra token; the
browser device flow is the only auth path.
```
> Note: exact filename/precedence (`AGENTS.md` vs `.hermes.md`) and whether SOUL.md or AGENTS.md is the more reliable trigger is set by Task 1 findings; adjust to the confirmed mechanism.

- [ ] **Step 4: Write `assets/hermes/start-dashboard.sh`**

```bash
#!/usr/bin/env bash
# Boot the guest-facing Hermes chat. Runs at sandbox start.
# WHY localhost + socat: binding the dashboard to 0.0.0.0 trips Hermes's Nous
# OAuth gate; we keep it on 127.0.0.1 (no auth) and expose it through socat on
# :8080, which Daytona's preview proxy serves as a clickable URL.
set -euo pipefail

# Inject runtime model choice (key already in ~/.hermes/.env via spawn).
if [ -n "${OPENROUTER_MODEL:-}" ]; then
  hermes config set model.default "${OPENROUTER_MODEL}" || true
fi

# Ensure the onboarding skill is present (source is swappable).
SKILL_TAP="${FULCRA_ONBOARDING_TAP:-fulcradynamics/agent-skills}"
SKILL_REF="${FULCRA_ONBOARDING_SKILL:-fulcradynamics/agent-skills/fulcra-onboarding}"
hermes skills tap add "${SKILL_TAP}" || true
hermes skills install "${SKILL_REF}" || true

# Start dashboard on localhost; forward to 0.0.0.0:8080 for the preview URL.
nohup hermes dashboard --host 127.0.0.1 --port 9119 --no-open >/tmp/hermes-dash.log 2>&1 &
nohup socat TCP-LISTEN:8080,fork,reuseaddr TCP:127.0.0.1:9119 >/tmp/socat.log 2>&1 &
wait
```
> Replace the dashboard/exposure line with the exact command confirmed in Task 1 if socat is not the chosen path.

- [ ] **Step 5: Commit**

```bash
git add assets/hermes/
git commit -m "feat: Hermes asset files (OpenRouter config, persona, onboarding directive, dashboard launcher)"
```

---

## Task 4: Declarative image definition (TDD for the param surface)

**Files:**
- Create: `src/fhd/image.py`, and extend `tests/` minimally

The image build itself is live-verified (Task 1 proved it). Here we encapsulate it as a function and unit-test the parts we can assert without a network call (that the asset files are referenced and commands are present).

- [ ] **Step 1: Write the failing test**

`tests/test_image.py`:
```python
from fhd.image import build_image_commands

def test_image_installs_uv_fulcra_hermes_and_socat():
    cmds = build_image_commands()
    joined = "\n".join(cmds)
    assert "astral.sh/uv/install.sh" in joined
    assert "uv tool install fulcra-api" in joined
    assert "hermes-agent/main/scripts/install.sh" in joined
    assert "--skip-browser --skip-setup" in joined
    assert "socat" in joined
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run --extra dev pytest tests/test_image.py -v`
Expected: FAIL (`ModuleNotFoundError: fhd.image`).

- [ ] **Step 3: Implement `src/fhd/image.py`**

```python
"""Declarative Daytona image for the demo sandbox.

Bakes uv + Fulcra CLI + Hermes (OpenRouter, no Playwright) + socat, and copies
the Hermes asset files into ~/.hermes. Secrets are NOT baked — only injected at
spawn. Built server-side by Daytona (no local Docker needed).
"""
from __future__ import annotations
from pathlib import Path
from daytona import Image

ASSETS = Path(__file__).resolve().parent.parent.parent / "assets" / "hermes"
HERMES_HOME = "/root/.hermes"   # confirm build user against Task 1 findings
PATH_ENV = "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

def build_image_commands() -> list[str]:
    """The shell commands baked into the image, in order. Separated out so it is
    unit-testable without a network call."""
    return [
        "apt-get update && apt-get install -y curl ca-certificates socat",
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "uv tool install fulcra-api",
        "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-browser --skip-setup",
        f"mkdir -p {HERMES_HOME}/skills",
        "chmod +x /opt/fhd/start-dashboard.sh",
    ]

def build_image() -> Image:
    img = Image.debian_slim("3.12").env({"PATH": PATH_ENV, "HERMES_HOME": HERMES_HOME})
    for cmd in build_image_commands()[:4]:
        img = img.run_commands(cmd)
    # copy assets into the image
    img = (
        img.add_local_file(str(ASSETS / "config.yaml"), f"{HERMES_HOME}/config.yaml")
        .add_local_file(str(ASSETS / "SOUL.md"), f"{HERMES_HOME}/SOUL.md")
        .add_local_file(str(ASSETS / "AGENTS.md"), f"{HERMES_HOME}/AGENTS.md")
        .add_local_file(str(ASSETS / "start-dashboard.sh"), "/opt/fhd/start-dashboard.sh")
    )
    for cmd in build_image_commands()[4:]:
        img = img.run_commands(cmd)
    return img
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run --extra dev pytest tests/test_image.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/fhd/image.py tests/test_image.py
git commit -m "feat: declarative Daytona image (uv + fulcra-api + Hermes + socat + assets)"
```

---

## Task 5: Snapshot build script

**Files:**
- Create: `src/fhd/build_snapshot.py`

- [ ] **Step 1: Implement `src/fhd/build_snapshot.py`**

```python
"""Build/register the reusable Daytona snapshot. Run once (and after image changes)."""
from __future__ import annotations
import argparse
from daytona import Daytona, DaytonaConfig, CreateSnapshotParams
from fhd.config import load_settings
from fhd.image import build_image

SNAPSHOT_NAME = "fhd-hermes-demo"

def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Hermes demo snapshot")
    ap.add_argument("--name", default=SNAPSHOT_NAME)
    args = ap.parse_args()
    s = load_settings()
    d = Daytona(DaytonaConfig(api_key=s.daytona_api_key, api_url=s.daytona_api_url, target=s.daytona_target))
    snap = d.snapshot.create(
        CreateSnapshotParams(name=args.name, image=build_image()),
        on_logs=lambda c: print(c, end=""),
    )
    print(f"\nSnapshot '{args.name}' state: {snap.state}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Live-verify the build**

Run: `uv run python -m fhd.build_snapshot`
Expected: build logs stream; final line `Snapshot 'fhd-hermes-demo' state: Active`. (If state is `Build Failed`, read the streamed logs, fix `image.py`/assets, re-run.)

- [ ] **Step 3: Commit**

```bash
git add src/fhd/build_snapshot.py
git commit -m "feat: snapshot build script (creates the fhd-hermes-demo snapshot)"
```

---

## Task 6: Spawn script (TDD for param building + live verify)

**Files:**
- Create: `src/fhd/snapshot_params.py`, `src/fhd/spawn.py`, `tests/test_snapshot_params.py`

- [ ] **Step 1: Write the failing test for param building**

`tests/test_snapshot_params.py`:
```python
from fhd.snapshot_params import build_spawn_kwargs

def test_build_spawn_kwargs_sets_env_idle_and_public():
    kw = build_spawn_kwargs(
        snapshot="fhd-hermes-demo",
        openrouter_api_key="sk-or-y",
        openrouter_model="anthropic/claude-sonnet-4.5",
        label="guest-alice",
    )
    assert kw["snapshot"] == "fhd-hermes-demo"
    assert kw["auto_stop_interval"] == 30
    assert kw["public"] is True
    assert kw["env_vars"]["OPENROUTER_API_KEY"] == "sk-or-y"
    assert kw["env_vars"]["OPENROUTER_MODEL"] == "anthropic/claude-sonnet-4.5"
    assert kw["labels"]["fhd"] == "guest"
    assert kw["labels"]["guest"] == "guest-alice"
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run --extra dev pytest tests/test_snapshot_params.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/fhd/snapshot_params.py`**

```python
"""Pure builders for Daytona sandbox params. Pure so they are unit-testable
without touching the network."""
from __future__ import annotations

AUTO_STOP_MINUTES = 30

def build_spawn_kwargs(*, snapshot: str, openrouter_api_key: str,
                       openrouter_model: str, label: str) -> dict:
    return {
        "snapshot": snapshot,
        "env_vars": {
            "OPENROUTER_API_KEY": openrouter_api_key,
            "OPENROUTER_MODEL": openrouter_model,
        },
        "auto_stop_interval": AUTO_STOP_MINUTES,
        "public": True,
        "labels": {"fhd": "guest", "guest": label},
    }
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run --extra dev pytest tests/test_snapshot_params.py -v`
Expected: 1 passed.

- [ ] **Step 5: Implement `src/fhd/spawn.py`**

```python
"""Spawn ONE guest sandbox and print a clickable preview URL.

Writes the OpenRouter key into the sandbox's ~/.hermes/.env, then starts the
dashboard launcher. Prints the URL to hand to the guest.
"""
from __future__ import annotations
import argparse
from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams, SessionExecuteRequest
from fhd.config import load_settings
from fhd.snapshot_params import build_spawn_kwargs

DASHBOARD_PORT = 8080

def main() -> None:
    ap = argparse.ArgumentParser(description="Spawn a guest Hermes sandbox")
    ap.add_argument("label", help="A label for this guest, e.g. 'alice'")
    ap.add_argument("--snapshot", default="fhd-hermes-demo")
    args = ap.parse_args()
    s = load_settings()
    d = Daytona(DaytonaConfig(api_key=s.daytona_api_key, api_url=s.daytona_api_url, target=s.daytona_target))

    kwargs = build_spawn_kwargs(
        snapshot=args.snapshot,
        openrouter_api_key=s.openrouter_api_key,
        openrouter_model=s.openrouter_model,
        label=args.label,
    )
    sb = d.create(CreateSandboxFromSnapshotParams(**kwargs), timeout=180)

    # Write OpenRouter key into Hermes .env (kept out of the image layer).
    sb.process.exec(
        "bash -lc 'mkdir -p ~/.hermes && printf \"OPENROUTER_API_KEY=%s\\n\" \"$OPENROUTER_API_KEY\" > ~/.hermes/.env'"
    )
    # Launch the guest-facing dashboard (backgrounded).
    sb.process.create_session("boot")
    sb.process.execute_session_command(
        "boot",
        SessionExecuteRequest(command="bash -lc '/opt/fhd/start-dashboard.sh'", run_async=True),
    )
    prev = sb.get_preview_link(DASHBOARD_PORT)
    print(f"\nGuest '{args.label}' is ready.")
    print(f"Sandbox id: {sb.id}")
    print(f"PRESS PLAY (send this link): {prev.url}")

if __name__ == "__main__":
    main()
```
> Confirm `sb.id` is the correct id attribute against Task 1 findings; adjust if it is `sb.id`/`sb.sandbox_id`.

- [ ] **Step 6: Live-verify the full guest flow**

Run: `uv run python -m fhd.spawn alice`
Expected: prints a `proxy.daytona.work` URL. Open it: the Hermes chat loads; on first message the agent starts onboarding, runs Fulcra `auth login`, relays a URL+code; completing device login with a throwaway Fulcra account makes `user-info` succeed. This must reproduce the Task 1 manual walkthrough.

- [ ] **Step 7: Commit**

```bash
git add src/fhd/snapshot_params.py src/fhd/spawn.py tests/test_snapshot_params.py
git commit -m "feat: spawn script — per-guest sandbox + clickable preview URL"
```

---

## Task 7: Teardown / list script (TDD for arg parsing + live verify)

**Files:**
- Create: `src/fhd/teardown.py`, `tests/test_cli_args.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cli_args.py`:
```python
from fhd.teardown import parse_args

def test_parse_args_list():
    a = parse_args(["--list"])
    assert a.list is True

def test_parse_args_delete_by_id():
    a = parse_args(["--delete", "sb_123"])
    assert a.delete == "sb_123"

def test_parse_args_delete_all_guests():
    a = parse_args(["--all"])
    assert a.all is True
```

- [ ] **Step 2: Run, verify fail**

Run: `uv run --extra dev pytest tests/test_cli_args.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/fhd/teardown.py`**

```python
"""List or delete guest sandboxes (labelled fhd=guest)."""
from __future__ import annotations
import argparse
from daytona import Daytona, DaytonaConfig
from fhd.config import load_settings

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="List/delete guest Hermes sandboxes")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="List guest sandboxes")
    g.add_argument("--delete", metavar="ID", help="Delete one sandbox by id")
    g.add_argument("--all", action="store_true", help="Delete ALL guest sandboxes")
    return ap.parse_args(argv)

def main() -> None:
    args = parse_args()
    s = load_settings()
    d = Daytona(DaytonaConfig(api_key=s.daytona_api_key, api_url=s.daytona_api_url, target=s.daytona_target))
    sandboxes = d.list()  # confirm list method/filter against Task 1 findings
    guests = [sb for sb in sandboxes if getattr(sb, "labels", {}).get("fhd") == "guest"]
    if args.list:
        for sb in guests:
            print(sb.id, getattr(sb, "labels", {}).get("guest"), getattr(sb, "state", "?"))
        print(f"{len(guests)} guest sandbox(es).")
        return
    if args.delete:
        d.get(args.delete).delete()  # confirm get/delete API against findings
        print(f"Deleted {args.delete}")
        return
    if args.all:
        for sb in guests:
            sb.delete()
            print(f"Deleted {sb.id}")
```
> The `d.list()` / `d.get(id)` calls and label filtering must be confirmed against Task 1 findings; adjust to the real SDK surface.

- [ ] **Step 4: Run, verify pass**

Run: `uv run --extra dev pytest tests/test_cli_args.py -v`
Expected: 3 passed.

- [ ] **Step 5: Live-verify**

Run: `uv run python -m fhd.teardown --list` then `uv run python -m fhd.teardown --all`
Expected: lists the `alice` sandbox from Task 6, then deletes it (verify it disappears from `--list`).

- [ ] **Step 6: Commit**

```bash
git add src/fhd/teardown.py tests/test_cli_args.py
git commit -m "feat: teardown/list script for guest sandboxes"
```

---

## Task 8: README + operator runbook

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

Cover (concise, human-readable for an outside reader): what this repo is and the demo thesis (ephemeral agent, permanent Fulcra memory); prerequisites (`.env` with `DAYTONA_API_KEY`, `OPENROUTER_API_KEY`, optional `OPENROUTER_MODEL`); one-time `uv run python -m fhd.build_snapshot`; per-guest `uv run python -m fhd.spawn <label>` → send the printed URL; cleanup `uv run python -m fhd.teardown --list|--all`; the 30-min idle auto-stop behavior and how to re-spawn; the demo-grade access note (public preview URL = only invitees get the link; ephemeral); how to swap the onboarding skill via `FULCRA_ONBOARDING_SKILL`/`FULCRA_ONBOARDING_TAP`; pointer to the spec, plan, and spike findings docs.

- [ ] **Step 2: Run the whole flow once against the README to confirm the instructions are accurate** (build → spawn → open → teardown).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: operator runbook / README for the Hermes-on-Daytona demo"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** image (T4/T5), no-secrets-in-image (T4 + spawn-time injection T6), OpenRouter strong model (assets T3 + config T2), onboarding skill swappable + auto-start (T3 + T1 findings), device-code flow (T1/T3/T6), per-user isolated spawn + signed/clickable URL (T6), 30-min idle (T6 param), teardown (T7), runbook (T8), ephemerality (public + auto-stop, no sandbox persistence). All spec sections map to tasks.
- **Placeholder scan:** no "TBD/handle errors" placeholders; uncertain SDK names are explicitly called out with "confirm against Task 1 findings" rather than left vague — the spike is the mechanism that resolves them.
- **Type consistency:** `build_spawn_kwargs`, `build_image_commands`, `load_settings`/`Settings`, `parse_args` names are used consistently across tasks and tests.
- **Known dependency:** Tasks 3–7 depend on Task 1 findings for a handful of exact SDK/Hermes names (preview id attr, `d.list()/d.get()`, dashboard exposure flag, onboarding trigger). This is deliberate: the spike converts the few remaining unknowns into pinned facts before they're built on.
