# Fulcra Attention — Plan A: Python Relay + CLI + Ingest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Python backend for `fulcra-attention` — a loopback HTTP relay that accepts browse pings on `127.0.0.1:8771`, scrubs them, and ingests them into Fulcra as DurationAnnotations under a new `Attention` definition. End-state: a `curl` POST against the running relay creates a real Fulcra annotation, idempotent under replay.

**Architecture:** Stdlib-only HTTP server (`http.server.ThreadingHTTPServer`) wrapped around a `FulcraClient` (modeled on `fulcra-media`). Auth via shell-out to `fulcra auth print-access-token` (with `FULCRA_ACCESS_TOKEN` env override for tests). Per-machine bearer token stored at `~/.config/fulcra-attention/relay.json` (mode 0600). Service registration via launchd (macOS) or systemd user units (Linux).

**Tech Stack:** Python 3.11+, `click` (CLI), `httpx` (Fulcra HTTP), stdlib `http.server` (relay), `fulcra-api` (auth), `pytest` + `httpx.MockTransport` (hermetic tests).

**Working directory:** `/Users/Scanning/Developer/fulcra-attention` (new repo, not yet created — Task 1 initializes it).

**Companion reference:** `/Users/Scanning/Developer/FulcraMediaHelpers/fulcra_media/` is the sibling codebase whose patterns this project mirrors (state.json shape, FulcraClient idioms, webhook receiver structure, test conventions).

**Spec:** `/Users/Scanning/Developer/FulcraMediaHelpers/docs/superpowers/specs/2026-05-18-fulcra-attention-v1-design.md`

**Validation target:** at end of Plan A, the user can:
1. `fulcra-attention bootstrap` → `Attention` def + tags exist in Fulcra
2. `fulcra-attention setup` → bearer token printed, launchd plist installed, relay running
3. `curl -X POST http://127.0.0.1:8771/attention -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"url":"https://example.com/article","title":"Manual smoke","category":null,"start_time":"2026-05-18T14:00:00Z","end_time":"2026-05-18T14:05:00Z","client":"curl/0.1","chrome_identity":"redacted@users.noreply.github.com","og_type":"article","lang":"en"}'` → a real DurationAnnotation appears in their Fulcra account, with `external_ids.chrome_identity`, `external_ids.og_type`, `external_ids.lang` carried through
4. Replay the same curl → silent no-op (source-id idempotency)

**Wire format (extension → relay):**
```json
{
  "url": "https://example.com/article",       // null when categorized
  "title": "Why I Quit Twitter",              // null when categorized
  "og_description": "A reflection on ...",    // null when missing
  "favicon_url": "https://example.com/fav.ico", // null when missing
  "category": null,                            // string slug when categorized

  // Context (all optional — null when unknown)
  "chrome_identity": "redacted@users.noreply.github.com", // Google account in this Chrome
                                                // profile, OR user-set label
  "og_type": "article",                        // <meta property="og:type">
  "lang": "en",                                 // <html lang="...">

  "start_time": "2026-05-18T14:23:08.412Z",
  "end_time":   "2026-05-18T14:35:42.108Z",
  "client": "fulcra-attention-chrome/0.1.0"
}
```
All four context fields land in `external_ids` on the Fulcra side. No new server-side tags are created — base tags stay `attention` + `web`.

---

## File Structure

```
fulcra-attention/
├── pyproject.toml
├── .gitignore
├── README.md
├── AGENTS.md
├── fulcra_attention/
│   ├── __init__.py
│   ├── state.py              # State dataclass + load/save (~/.config/fulcra-attention/state.json)
│   ├── fulcra.py             # FulcraClient: auth, ensure_tag, ensure_definitions, ingest_batch
│   ├── scrub.py              # Tier 1 param-strip (pure function)
│   ├── ingest.py             # build_attention_event, source-id derivation
│   ├── relay.py              # ThreadingHTTPServer on 127.0.0.1:8771
│   ├── service_manager.py    # launchd plist + systemd user unit generation
│   └── cli.py                # click entry: bootstrap, setup, status, reset, relay
└── tests/
    ├── conftest.py           # shared fixtures (FulcraClient with MockTransport)
    ├── test_state.py
    ├── test_fulcra_auth.py
    ├── test_fulcra_tags_defs.py
    ├── test_fulcra_ingest.py
    ├── test_scrub.py         # ~50 table-driven cases
    ├── test_ingest_event.py  # build_attention_event shape + source-id
    ├── test_relay.py         # happy path, bearer auth, schema validation
    ├── test_service_manager.py
    └── test_cli.py
```

---

## Task 1: Repo scaffolding

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/pyproject.toml`
- Create: `/Users/Scanning/Developer/fulcra-attention/.gitignore`
- Create: `/Users/Scanning/Developer/fulcra-attention/README.md`
- Create: `/Users/Scanning/Developer/fulcra-attention/AGENTS.md`
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/__init__.py`
- Create: `/Users/Scanning/Developer/fulcra-attention/tests/__init__.py`
- Create: `/Users/Scanning/Developer/fulcra-attention/tests/conftest.py`

- [ ] **Step 1: Initialize the repo**

```bash
mkdir -p /Users/Scanning/Developer/fulcra-attention/{fulcra_attention,tests}
cd /Users/Scanning/Developer/fulcra-attention
git init
git remote add origin https://github.com/ashfulcra/fulcra-attention.git  # repo created later via gh
```

- [ ] **Step 2: Write pyproject.toml**

```toml
# /Users/Scanning/Developer/fulcra-attention/pyproject.toml
[project]
name = "fulcra-attention"
version = "0.1.0"
description = "Capture what takes your attention while browsing, into your own Fulcra account."
readme = "README.md"
requires-python = ">=3.11"
authors = [{name = "ash", email = "redacted@users.noreply.github.com"}]
dependencies = [
    "click>=8.1",
    "httpx>=0.27",
    "fulcra-api>=0.5",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
]

[project.scripts]
fulcra-attention = "fulcra_attention.cli:cli"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["fulcra_attention*"]
```

- [ ] **Step 3: Write .gitignore**

```gitignore
# /Users/Scanning/Developer/fulcra-attention/.gitignore
__pycache__/
*.py[cod]
.venv/
.venv-*/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/
.scratch/
.claude/
.DS_Store
~/.config/fulcra-attention/
```

- [ ] **Step 4: Write minimal README.md**

```markdown
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
```

- [ ] **Step 5: Write AGENTS.md and package __init__**

```markdown
# /Users/Scanning/Developer/fulcra-attention/AGENTS.md
# AGENTS.md — autoloaded by Aider / Cursor / Continue.dev / Claude Code / OpenHands

This repo's agent-facing skill will live at `skills/fulcra-attention/SKILL.md` (added in Plan B). For now: this is the Python relay backend for fulcra-attention. See `pyproject.toml` for entry points.
```

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/__init__.py
"""fulcra-attention — capture browsing attention into Fulcra."""
__version__ = "0.1.0"
```

```python
# /Users/Scanning/Developer/fulcra-attention/tests/__init__.py
```

- [ ] **Step 6: Write tests/conftest.py with the shared MockTransport fixture**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/conftest.py
"""Shared pytest fixtures."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


@pytest.fixture
def recording_transport() -> Callable[..., httpx.MockTransport]:
    """Factory for a MockTransport that records every outgoing request.

    Usage:
        def test_x(recording_transport):
            transport = recording_transport(lambda r: httpx.Response(200, json={"id": "x"}))
            # transport.requests is the list of httpx.Request objects observed
    """
    def _factory(
        responder: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.MockTransport:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return responder(request)

        t = httpx.MockTransport(handler)
        t.requests = requests  # type: ignore[attr-defined]
        return t

    return _factory
```

- [ ] **Step 7: Install + verify the package imports**

```bash
cd /Users/Scanning/Developer/fulcra-attention
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -c "import fulcra_attention; print(fulcra_attention.__version__)"
```
Expected: `0.1.0`

- [ ] **Step 8: Commit**

```bash
cd /Users/Scanning/Developer/fulcra-attention
git add pyproject.toml .gitignore README.md AGENTS.md fulcra_attention/ tests/
git commit -m "chore: scaffold fulcra-attention repo (Python backend)"
```

---

## Task 2: State module

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/state.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_state.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_state.py
"""Tests for state.py."""
from __future__ import annotations

import json
from pathlib import Path

from fulcra_attention.state import State, load, save


def test_load_returns_empty_state_when_file_missing(tmp_path: Path):
    p = tmp_path / "state.json"
    assert load(p) == State()


def test_save_then_load_roundtrips(tmp_path: Path):
    p = tmp_path / "state.json"
    s = State(
        attention_definition_id="def-123",
        tag_ids={"attention": "tag-att", "web": "tag-web"},
        watermarks={"chrome": "2026-05-18T12:00:00Z"},
    )
    save(s, p)
    assert load(p) == s


def test_save_writes_mode_aware_parent_dir(tmp_path: Path):
    p = tmp_path / "nested" / "deep" / "state.json"
    save(State(attention_definition_id="x"), p)
    assert p.exists()


def test_load_tolerates_missing_optional_fields(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"attention_definition_id": "def-x"}))
    s = load(p)
    assert s.attention_definition_id == "def-x"
    assert s.tag_ids == {}
    assert s.watermarks == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/Scanning/Developer/fulcra-attention
.venv/bin/pytest tests/test_state.py -v
```
Expected: 4 FAIL (ModuleNotFoundError: fulcra_attention.state)

- [ ] **Step 3: Implement state.py**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/state.py
"""On-disk state for fulcra-attention.

Mirrors fulcra-media's state.py pattern. One Attention DurationAnnotation
definition; per-client watermarks (highest end_time seen); cached tag UUIDs.

Default location: ~/.config/fulcra-attention/state.json. Every function
takes an explicit path argument for hermetic tests.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(
    os.environ.get("FULCRA_ATTENTION_STATE")
    or os.path.expanduser("~/.config/fulcra-attention/state.json")
)


@dataclass
class State:
    attention_definition_id: str | None = None
    tag_ids: dict[str, str] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)


def load(path: Path = DEFAULT_PATH) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    return State(
        attention_definition_id=raw.get("attention_definition_id"),
        tag_ids=raw.get("tag_ids", {}),
        watermarks=raw.get("watermarks", {}),
    )


def save(state: State, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_state.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/state.py tests/test_state.py
git commit -m "feat(state): on-disk state.json with definition_id/tags/watermarks"
```

---

## Task 3: FulcraClient — auth

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/fulcra.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_auth.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_auth.py
"""Auth path: env override, shell-out, error surface."""
from __future__ import annotations

import subprocess

import pytest

from fulcra_attention.fulcra import FulcraClient


def test_env_override_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-env-tok")
    client = FulcraClient()
    assert client.get_token() == "test-env-tok"


def test_shell_out_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, mocker
):
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    fake = mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"shell-tok\n"
        ),
    )
    assert FulcraClient().get_token() == "shell-tok"
    fake.assert_called_once_with(
        ["fulcra", "auth", "print-access-token"],
        check=True,
        capture_output=True,
    )


def test_shell_out_failure_raises_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, mocker
):
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    mocker.patch(
        "fulcra_attention.fulcra.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=[], stderr=b"not logged in"
        ),
    )
    with pytest.raises(RuntimeError, match="fulcra auth print-access-token failed"):
        FulcraClient().get_token()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_fulcra_auth.py -v
```
Expected: 3 FAIL (ModuleNotFoundError: fulcra_attention.fulcra)

- [ ] **Step 3: Implement fulcra.py (auth slice only)**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/fulcra.py
"""Fulcra API client for fulcra-attention.

Single point of contact with the Fulcra REST API. Mirrors
fulcra-media/fulcra.py's shape: subprocess-shell-out auth, ensure_tag,
ensure_definitions, ingest_batch. Different annotation type (just
Attention) so we keep it standalone rather than importing from fulcra-media.
"""
from __future__ import annotations

import os
import subprocess

import httpx

from .state import State

DEFAULT_BASE_URL = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


class FulcraClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._transport = transport
        self._http: httpx.Client | None = None

    def get_token(self) -> str:
        env = os.environ.get("FULCRA_ACCESS_TOKEN")
        if env:
            return env
        try:
            result = subprocess.run(
                ["fulcra", "auth", "print-access-token"],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                "fulcra auth print-access-token failed; run `fulcra auth login` first. "
                f"stderr={exc.stderr!r}"
            ) from exc
        return result.stdout.decode().strip()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=self._transport,
                timeout=30.0,
                headers={"User-Agent": "fulcra-attention/0.1"},
                follow_redirects=True,
            )
        return self._http

    def _authed_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_fulcra_auth.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/fulcra.py tests/test_fulcra_auth.py
git commit -m "feat(fulcra): client auth (env override + shell-out via fulcra-api CLI)"
```

---

## Task 4: FulcraClient — tags + definitions

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/fulcra.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_tags_defs.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_tags_defs.py
"""Tag + definition bootstrap (idempotent)."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.state import State


@pytest.fixture(autouse=True)
def _force_test_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")


def test_ensure_tag_creates_when_missing(recording_transport):
    posted = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "tag-new"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("attention", state)
    assert tag_id == "tag-new"
    assert state.tag_ids["attention"] == "tag-new"
    assert posted == [{"name": "attention"}]


def test_ensure_tag_reuses_cache(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"attention": "tag-cached"})
    assert client.ensure_tag("attention", state) == "tag-cached"


def test_ensure_tag_uses_existing_server_side(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/tag/name/web":
            return httpx.Response(200, json={"id": "tag-existing"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    assert client.ensure_tag("web", state) == "tag-existing"
    assert state.tag_ids["web"] == "tag-existing"


def test_ensure_definitions_creates_attention_def(recording_transport):
    posted_defs: list[dict] = []
    posted_tags: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            body = json.loads(r.content)
            posted_tags.append(body)
            return httpx.Response(200, json={"id": f"tag-{body['name']}"})
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted_defs.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-attention"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    client.ensure_definitions(state)
    assert state.attention_definition_id == "def-attention"
    assert {t["name"] for t in posted_tags} == {"attention", "web"}
    assert len(posted_defs) == 1
    d = posted_defs[0]
    assert d["name"] == "Attention"
    assert d["annotation_type"] == "duration"
    assert "tag-attention" in d["tags"] and "tag-web" in d["tags"]


def test_ensure_definitions_skips_when_already_cached(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(
        attention_definition_id="def-x",
        tag_ids={"attention": "a", "web": "w"},
    )
    client.ensure_definitions(state)
    assert state.attention_definition_id == "def-x"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_fulcra_tags_defs.py -v
```
Expected: 5 FAIL (AttributeError: 'FulcraClient' object has no attribute 'ensure_tag')

- [ ] **Step 3: Append ensure_tag + ensure_definitions to fulcra.py**

Add to `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/fulcra.py`:

```python
    def ensure_tag(self, name: str, state: State) -> str:
        if name in state.tag_ids:
            return state.tag_ids[name]
        c = self._client()
        r = c.get(f"/user/v1alpha1/tag/name/{name}", headers=self._authed_headers())
        if r.status_code == 200:
            tag_id = r.json()["id"]
        else:
            r = c.post(
                "/user/v1alpha1/tag",
                json={"name": name},
                headers=self._authed_headers(),
            )
            r.raise_for_status()
            tag_id = r.json()["id"]
        state.tag_ids[name] = tag_id
        return tag_id

    def ensure_definitions(self, state: State) -> None:
        if state.attention_definition_id:
            return
        attention = self.ensure_tag("attention", state)
        web = self.ensure_tag("web", state)
        body = {
            "annotation_type": "duration",
            "name": "Attention",
            "description": "What the user paid attention to (browsing).",
            "tags": [attention, web],
            "measurement_spec": {
                "measurement_type": "duration",
                "value_type": "duration",
                "unit": None,
            },
        }
        r = self._client().post(
            "/user/v1alpha1/annotation",
            json=body,
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        state.attention_definition_id = r.json()["id"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_fulcra_tags_defs.py -v
```
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/fulcra.py tests/test_fulcra_tags_defs.py
git commit -m "feat(fulcra): ensure_tag + ensure_definitions (Attention DurationAnnotation def)"
```

---

## Task 5: FulcraClient — ingest_batch

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/fulcra.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_ingest.py`

- [ ] **Step 1: Write failing test (byte-for-byte JSONL payload assertion)**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_fulcra_ingest.py
"""Ingest pipeline — exact wire format assertion."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.state import State


@pytest.fixture(autouse=True)
def _force_test_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")


def test_ingest_batch_posts_jsonl_to_record_batch(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "POST" and r.url.path == "/ingest/v1/record/batch":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )
    event = {
        "specversion": 1,
        "data": json.dumps({"note": "Attention: Test", "title": "Test"}, sort_keys=True),
        "metadata": {
            "data_type": "DurationAnnotation",
            "recorded_at": {
                "start_time": "2026-05-18T14:00:00Z",
                "end_time":   "2026-05-18T14:05:00Z",
            },
            "tags": ["tag-a", "tag-w"],
            "source": [
                "com.fulcra.attention.v1.0123456789abcdef",
                "com.fulcradynamics.annotation.def-att",
            ],
            "content_type": "application/json",
        },
    }
    client.ingest_batch([event])
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.headers["content-type"] == "application/x-jsonl"
    assert sent.headers["authorization"] == "Bearer test-tok"
    # One JSON object per line; sorted keys for determinism
    expected_line = json.dumps(event, sort_keys=True).encode()
    assert sent.content == expected_line


def test_ingest_batch_no_op_on_empty(recording_transport):
    transport = recording_transport(lambda r: pytest.fail("unexpected"))
    client = FulcraClient(transport=transport)
    client.ingest_batch([])
    assert transport.requests == []


def test_ingest_batch_two_events_joined_by_newline(recording_transport):
    transport = recording_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    client = FulcraClient(transport=transport)
    a = {"specversion": 1, "data": "a", "metadata": {"x": 1}}
    b = {"specversion": 1, "data": "b", "metadata": {"x": 2}}
    client.ingest_batch([a, b])
    body = transport.requests[0].content
    assert body == json.dumps(a, sort_keys=True).encode() + b"\n" + json.dumps(b, sort_keys=True).encode()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_fulcra_ingest.py -v
```
Expected: 3 FAIL (AttributeError: 'FulcraClient' object has no attribute 'ingest_batch')

- [ ] **Step 3: Append ingest_batch to fulcra.py**

```python
    def ingest_batch(self, events: list[dict]) -> None:
        """POST a JSONL batch of already-built events to /ingest/v1/record/batch.

        Each event must be a dict with `specversion`, `data`, `metadata` keys
        (the wire format documented in the spec). Source-id idempotency is the
        caller's responsibility — building the deterministic source-id lives
        in ingest.py.
        """
        import json as _json  # local to avoid shadowing
        if not events:
            return
        lines = [_json.dumps(e, sort_keys=True).encode() for e in events]
        body = b"\n".join(lines)
        r = self._client().post(
            "/ingest/v1/record/batch",
            content=body,
            headers={
                **self._authed_headers(),
                "content-type": "application/x-jsonl",
            },
        )
        r.raise_for_status()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_fulcra_ingest.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/fulcra.py tests/test_fulcra_ingest.py
git commit -m "feat(fulcra): ingest_batch — JSONL POST to /ingest/v1/record/batch"
```

---

## Task 6: scrub.py — Tier 1 param-strip

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/scrub.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_scrub.py`

- [ ] **Step 1: Write failing tests (table-driven, ~50 cases)**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_scrub.py
"""Tier 1 param-strip: drop auth/tracking params from query+fragment."""
from __future__ import annotations

import pytest

from fulcra_attention.scrub import scrub_url

# (input, expected) table. Keep alphabetized within each section.
CASES = [
    # ---- Auth-bearing ----
    ("https://x.com/p?access_token=abc",     "https://x.com/p"),
    ("https://x.com/p?code=ABC&state=xyz",   "https://x.com/p"),
    ("https://x.com/p?apikey=k",             "https://x.com/p"),
    ("https://x.com/p?api_key=k",            "https://x.com/p"),
    ("https://x.com/p?key=k",                "https://x.com/p"),
    ("https://x.com/p?token=t",              "https://x.com/p"),
    ("https://x.com/p?authorization=a",      "https://x.com/p"),
    ("https://x.com/p?id_token=jwt",         "https://x.com/p"),
    ("https://x.com/p?refresh_token=r",      "https://x.com/p"),
    ("https://x.com/p?nonce=n",              "https://x.com/p"),
    ("https://x.com/p?client_secret=cs",     "https://x.com/p"),
    ("https://x.com/p?assertion=a",          "https://x.com/p"),
    ("https://x.com/p?session=s",            "https://x.com/p"),
    ("https://x.com/p?sid=s",                "https://x.com/p"),
    ("https://x.com/p?sessionid=s",          "https://x.com/p"),
    ("https://x.com/p?auth=a",               "https://x.com/p"),
    ("https://x.com/p?signature=s",          "https://x.com/p"),
    ("https://x.com/p?sig=s",                "https://x.com/p"),
    ("https://x.com/p?hmac=h",               "https://x.com/p"),
    ("https://x.com/p?password=p",           "https://x.com/p"),
    ("https://x.com/p?pwd=p",                "https://x.com/p"),
    ("https://x.com/p?otp=123",              "https://x.com/p"),
    ("https://x.com/p?magic=m",              "https://x.com/p"),
    ("https://x.com/p?share_token=s",        "https://x.com/p"),
    ("https://x.com/p?invite=i",             "https://x.com/p"),
    ("https://x.com/p?confirmation_token=c", "https://x.com/p"),
    ("https://x.com/p?_csrf=c",              "https://x.com/p"),
    ("https://x.com/p?csrf_token=c",         "https://x.com/p"),
    ("https://x.com/p?xsrf=x",               "https://x.com/p"),
    ("https://x.com/p?ticket=t",             "https://x.com/p"),
    ("https://x.com/p?ott=o",                "https://x.com/p"),
    # ---- AWS signed URLs ----
    ("https://s3.aws.com/bucket/k?X-Amz-Signature=abc&X-Amz-Credential=c&X-Amz-Security-Token=t&Expires=123",
     "https://s3.aws.com/bucket/k"),
    # ---- Tracking ----
    ("https://x.com/p?utm_source=newsletter", "https://x.com/p"),
    ("https://x.com/p?utm_medium=email&utm_campaign=launch", "https://x.com/p"),
    ("https://x.com/p?gclid=g",               "https://x.com/p"),
    ("https://x.com/p?fbclid=f",              "https://x.com/p"),
    ("https://x.com/p?msclkid=m",             "https://x.com/p"),
    ("https://x.com/p?mc_eid=e&mc_cid=c",     "https://x.com/p"),
    ("https://x.com/p?_hsenc=h&_hsmi=h",      "https://x.com/p"),
    ("https://x.com/p?igshid=i",              "https://x.com/p"),
    ("https://x.com/p?yclid=y",               "https://x.com/p"),
    # ---- One-click action ----
    ("https://x.com/p?unsubscribe=u",         "https://x.com/p"),
    ("https://x.com/p?verify=v",              "https://x.com/p"),
    ("https://x.com/p?reset=r",               "https://x.com/p"),
    ("https://x.com/p?confirm=c",             "https://x.com/p"),
    ("https://x.com/p?activate=a",            "https://x.com/p"),
    # ---- Case-insensitivity ----
    ("https://x.com/p?ACCESS_TOKEN=a",        "https://x.com/p"),
    ("https://x.com/p?Code=c",                "https://x.com/p"),
    # ---- Legit params preserved ----
    ("https://x.com/p?q=foo&page=2",          "https://x.com/p?q=foo&page=2"),
    ("https://x.com/p?id=123",                "https://x.com/p?id=123"),
    # ---- Mixed: some stripped, some preserved ----
    ("https://x.com/p?id=1&access_token=t&page=2", "https://x.com/p?id=1&page=2"),
    # ---- Fragment dropped by default ----
    ("https://x.com/p#section1",              "https://x.com/p"),
    ("https://x.com/p?id=1#access_token=t",   "https://x.com/p?id=1"),
    # ---- No path ----
    ("https://x.com/?utm_source=x",           "https://x.com/"),
    # ---- No query, no fragment ----
    ("https://x.com/p",                       "https://x.com/p"),
]


@pytest.mark.parametrize("raw,expected", CASES, ids=[c[0] for c in CASES])
def test_scrub_url(raw: str, expected: str):
    assert scrub_url(raw) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_scrub.py -v
```
Expected: All FAIL (ModuleNotFoundError: fulcra_attention.scrub)

- [ ] **Step 3: Implement scrub.py**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/scrub.py
"""Tier 1 URL scrubbing — pure function.

Strip auth-bearing query params, tracking params, one-click action tokens.
Whole fragment dropped by default (covers OAuth Implicit Flow + Slack/Notion
magic-share links).

Cross-language contract: a sibling TypeScript implementation in the Chrome
extension (Plan B) must produce identical output for identical input.
Shared fixture file lives in `tests/fixtures/scrub_cases.json` (Plan B).
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Lowercase for case-insensitive matching.
DENYLIST: frozenset[str] = frozenset({
    # auth-bearing
    "access_token", "id_token", "refresh_token", "code", "state", "nonce",
    "client_secret", "assertion", "session", "sid", "sessionid", "auth",
    "authorization", "token", "apikey", "api_key", "key", "signature",
    "sig", "hmac", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
    "magic", "share_token", "invite", "confirmation_token",
    "_csrf", "csrf_token", "xsrf", "ticket", "ott",
    # tracking
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
    "igshid", "yclid", "ref", "ref_src", "ref_url",
    # one-click action tokens
    "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
})


def scrub_url(url: str) -> str:
    """Return `url` with denylisted query params and the entire fragment dropped.

    Pure function. Preserves param order of surviving entries.
    """
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for (k, v) in pairs if k.lower() not in DENYLIST]
    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_scrub.py -v
```
Expected: All PASS (50+ cases)

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/scrub.py tests/test_scrub.py
git commit -m "feat(scrub): Tier 1 param-strip + fragment-drop (50+ table-driven cases)"
```

---

## Task 7: ingest.py — build_attention_event

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/ingest.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_ingest_event.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_ingest_event.py
"""build_attention_event — payload shape + source-id determinism."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fulcra_attention.ingest import build_attention_event, source_id
from fulcra_attention.state import State


@pytest.fixture
def state() -> State:
    return State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )


def test_source_id_deterministic_for_same_url_and_second():
    t1 = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 18, 14, 0, 0, 500_000, tzinfo=timezone.utc)  # same second
    a = source_id(key="https://x.com/p", start_time=t1)
    b = source_id(key="https://x.com/p", start_time=t2)
    assert a == b
    assert a.startswith("com.fulcra.attention.v1.")


def test_source_id_changes_when_url_changes():
    t = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    assert source_id(key="a", start_time=t) != source_id(key="b", start_time=t)


def test_source_id_changes_when_second_changes():
    a = source_id(key="x", start_time=datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc))
    b = source_id(key="x", start_time=datetime(2026, 5, 18, 14, 0, 1, tzinfo=timezone.utc))
    assert a != b


def test_build_event_url_variant(state: State):
    payload = {
        "url": "https://example.com/article",
        "title": "Why I Quit Twitter",
        "og_description": "A reflection.",
        "favicon_url": "https://example.com/fav.ico",
        "category": None,
        "chrome_identity": "redacted@users.noreply.github.com",
        "og_type": "article",
        "lang": "en",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "fulcra-attention-chrome/0.1.0",
    }
    ev = build_attention_event(payload, state=state)
    assert ev["specversion"] == 1
    md = ev["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"]["start_time"] == "2026-05-18T14:00:00Z"
    assert md["recorded_at"]["end_time"] == "2026-05-18T14:05:00Z"
    assert md["tags"] == ["tag-a", "tag-w"]
    assert md["source"][0].startswith("com.fulcra.attention.v1.")
    assert md["source"][1] == "com.fulcradynamics.annotation.def-att"
    data = json.loads(ev["data"])
    assert data["title"] == "Why I Quit Twitter"
    assert data["url"] == "https://example.com/article"
    assert data["category"] is None
    assert data["og_description"] == "A reflection."
    assert data["favicon_url"] == "https://example.com/fav.ico"
    assert data["service"] == "web"
    assert data["parent_source_id"] is None
    assert data["external_ids"]["host"] == "example.com"
    assert data["external_ids"]["client"] == "fulcra-attention-chrome/0.1.0"
    assert data["external_ids"]["chrome_identity"] == "redacted@users.noreply.github.com"
    assert data["external_ids"]["og_type"] == "article"
    assert data["external_ids"]["lang"] == "en"
    assert data["note"] == "Attention: Why I Quit Twitter"


def test_build_event_omits_unknown_enrichment_fields(state: State):
    """When chrome_identity / og_type / lang are missing from payload,
    they go in external_ids as None — never KeyError."""
    payload = {
        "url": "https://example.com/article",
        "title": "T",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(payload, state=state)
    data = json.loads(ev["data"])
    assert data["external_ids"]["chrome_identity"] is None
    assert data["external_ids"]["og_type"] is None
    assert data["external_ids"]["lang"] is None


def test_build_event_category_variant(state: State):
    payload = {
        "url": None,
        "title": None,
        "og_description": None,
        "favicon_url": None,
        "category": "banking",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "fulcra-attention-chrome/0.1.0",
    }
    ev = build_attention_event(payload, state=state)
    data = json.loads(ev["data"])
    assert data["category"] == "banking"
    assert data["url"] is None
    assert data["title"] is None
    assert data["external_ids"].get("host") is None
    assert data["note"] == "Attention: banking"


def test_build_event_source_id_url_keyed_when_url_present(state: State):
    p = {
        "url": "https://example.com/x",
        "title": "x",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev1 = build_attention_event(p, state=state)
    ev2 = build_attention_event(p, state=state)
    assert ev1["metadata"]["source"][0] == ev2["metadata"]["source"][0]


def test_build_event_source_id_category_keyed_when_categorized(state: State):
    p = {
        "url": None,
        "title": None,
        "category": "banking",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    # source-id derived from "banking" + start_time, NOT from any URL
    assert ev["metadata"]["source"][0].startswith("com.fulcra.attention.v1.")


def test_build_event_strips_fractional_seconds_from_recorded_at(state: State):
    p = {
        "url": "https://x.com/",
        "title": "X",
        "category": None,
        "start_time": "2026-05-18T14:00:00.412Z",
        "end_time":   "2026-05-18T14:05:00.108Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["recorded_at"]["start_time"] == "2026-05-18T14:00:00Z"
    assert ev["metadata"]["recorded_at"]["end_time"] == "2026-05-18T14:05:00Z"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_ingest_event.py -v
```
Expected: 7 FAIL (ModuleNotFoundError: fulcra_attention.ingest)

- [ ] **Step 3: Implement ingest.py**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/ingest.py
"""Build DurationAnnotation events for fulcra-attention.

Converts a single relay-shaped payload to the wire format ingested by
FulcraClient.ingest_batch. Source-id is sha256-derived for idempotency.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .state import State

SOURCE_PREFIX = "com.fulcra.attention.v1."


def _parse_iso(s: str) -> datetime:
    """Tolerant ISO-8601 parse. Accepts both 'Z' and '+00:00'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_second_iso(s: str) -> str:
    """Truncate fractional seconds, render with trailing 'Z'."""
    dt = _parse_iso(s).replace(microsecond=0)
    out = dt.isoformat()
    return out.replace("+00:00", "Z")


def source_id(*, key: str, start_time: datetime) -> str:
    """Deterministic source-id derived from `key` (URL or category) and
    `start_time` truncated to the second."""
    sec = start_time.replace(microsecond=0).isoformat()
    h = hashlib.sha256(f"{key}|{sec}".encode()).hexdigest()
    return f"{SOURCE_PREFIX}{h[:16]}"


def build_attention_event(payload: dict, *, state: State) -> dict:
    """Translate a relay-validated payload to a DurationAnnotation wire dict.

    Caller has already enforced: exactly one of {url, category} non-null,
    bearer token, time bounds. We trust the payload here.
    """
    url = payload.get("url")
    category = payload.get("category")
    title = payload.get("title")
    og_description = payload.get("og_description")
    favicon_url = payload.get("favicon_url")
    client = payload["client"]
    chrome_identity = payload.get("chrome_identity")
    og_type = payload.get("og_type")
    lang = payload.get("lang")
    start_time = _to_second_iso(payload["start_time"])
    end_time = _to_second_iso(payload["end_time"])

    if url is not None:
        host: str | None = urlsplit(url).hostname
        note = f"Attention: {title or url}"
        sid_key = url
    else:
        host = None
        note = f"Attention: {category}"
        sid_key = category or ""

    start_dt = _parse_iso(payload["start_time"])
    sid = source_id(key=sid_key, start_time=start_dt)

    data_inner: dict[str, Any] = {
        "note": note,
        "title": title,
        "service": "web",
        "category": category,
        "url": url,
        "og_description": og_description,
        "favicon_url": favicon_url,
        "parent_source_id": None,  # reserved for v2 highlights
        "external_ids": {
            "client": client,
            "host": host,
            "chrome_identity": chrome_identity,
            "og_type": og_type,
            "lang": lang,
        },
    }
    assert state.attention_definition_id, "ensure_definitions() must run first"
    metadata = {
        "data_type": "DurationAnnotation",
        "recorded_at": {
            "start_time": start_time,
            "end_time": end_time,
        },
        "tags": [
            state.tag_ids["attention"],
            state.tag_ids["web"],
        ],
        "source": [
            sid,
            f"com.fulcradynamics.annotation.{state.attention_definition_id}",
        ],
        "content_type": "application/json",
    }
    return {
        "specversion": 1,
        "data": json.dumps(data_inner, sort_keys=True),
        "metadata": metadata,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_ingest_event.py -v
```
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/ingest.py tests/test_ingest_event.py
git commit -m "feat(ingest): build_attention_event + source_id (deterministic, second-truncated)"
```

---

## Task 8: relay.py — happy-path POST /attention

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/relay.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_relay.py`

- [ ] **Step 1: Write failing tests (happy path, no auth yet)**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_relay.py
"""Relay HTTP endpoint — exercised via stdlib http.client against a live server."""
from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.relay import ReceiverContext, make_server
from fulcra_attention.state import State


@pytest.fixture
def state() -> State:
    return State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )


@pytest.fixture
def client_with_ingest_capture(recording_transport):
    """FulcraClient whose /ingest/v1/record/batch always 200s."""
    transport = recording_transport(
        lambda r: httpx.Response(200, json={"ok": True})
    )
    return FulcraClient(transport=transport)


@pytest.fixture
def running_server(state, client_with_ingest_capture, monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    ctx = ReceiverContext(
        client=client_with_ingest_capture,
        state=state,
        bearer_token="test-bearer",
    )
    server = make_server(host="127.0.0.1", port=0, context=ctx)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server, port, ctx
    finally:
        server.shutdown()
        server.server_close()


def _post(port: int, body: dict, *, token: str = "test-bearer") -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST", "/attention",
        body=json.dumps(body),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    resp = conn.getresponse()
    status = resp.status
    payload = json.loads(resp.read())
    conn.close()
    return status, payload


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_post_attention_happy_path_url(running_server, client_with_ingest_capture):
    _server, port, ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    start = now - timedelta(minutes=5)
    status, payload = _post(port, {
        "url": "https://example.com/article",
        "title": "Test",
        "category": None,
        "chrome_identity": "redacted@users.noreply.github.com",
        "og_type": "article",
        "lang": "en",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time":   end.isoformat().replace("+00:00", "Z"),
        "client": "curl/0.1",
    })
    assert status == 200, payload
    assert payload["posted"] == 1
    # FulcraClient saw exactly one ingest POST
    transport = client_with_ingest_capture._transport
    assert len(transport.requests) == 1
    sent_body = transport.requests[0].content
    line = json.loads(sent_body)
    assert line["metadata"]["data_type"] == "DurationAnnotation"
    inner = json.loads(line["data"])
    assert inner["title"] == "Test"
    assert inner["url"] == "https://example.com/article"
    # Context flowed through to external_ids
    assert inner["external_ids"]["chrome_identity"] == "redacted@users.noreply.github.com"
    assert inner["external_ids"]["og_type"] == "article"
    assert inner["external_ids"]["lang"] == "en"


def test_post_attention_happy_path_category(running_server, client_with_ingest_capture):
    _server, port, ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    start = now - timedelta(minutes=2)
    status, payload = _post(port, {
        "url": None,
        "title": None,
        "category": "banking",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time":   end.isoformat().replace("+00:00", "Z"),
        "client": "curl/0.1",
    })
    assert status == 200
    inner = json.loads(json.loads(client_with_ingest_capture._transport.requests[0].content)["data"])
    assert inner["category"] == "banking"
    assert inner["url"] is None


def test_post_unknown_path_404s(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/garbage", body="{}",
                 headers={"Authorization": "Bearer test-bearer",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: 3 FAIL (ModuleNotFoundError: fulcra_attention.relay)

- [ ] **Step 3: Implement relay.py (happy path only — auth/validation in next tasks)**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/relay.py
"""Loopback HTTP relay — accepts browse pings from the Chrome extension.

Stdlib-only (http.server.ThreadingHTTPServer). Mirrors the shape of
fulcra-media's webhook_receiver but with a different endpoint and
payload schema. Bearer-token authentication required.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .fulcra import FulcraClient
from .ingest import build_attention_event
from .state import State


class ReceiverContext:
    """Thread-safe shared state for the relay."""

    def __init__(
        self,
        *,
        client: FulcraClient,
        state: State,
        bearer_token: str,
    ) -> None:
        self.client = client
        self.state = state
        self.bearer_token = bearer_token
        self._lock = threading.Lock()
        self.received = 0
        self.posted = 0
        self.dropped = 0

    def bump(self, *, posted: int = 0, dropped: int = 0) -> None:
        with self._lock:
            self.received += 1
            self.posted += posted
            self.dropped += dropped

    def health(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "definition_id": self.state.attention_definition_id,
                "received": self.received,
                "posted": self.posted,
                "dropped": self.dropped,
            }


class AttentionHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # quiet by default
        return

    def _context(self) -> ReceiverContext:
        return self.server.context  # type: ignore[attr-defined]

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, self._context().health())
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/attention":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        ctx = self._context()

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad content-length"})
            return
        body = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"ok": False, "error": "bad json", "message": str(exc)})
            return

        try:
            event = build_attention_event(payload, state=ctx.state)
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": "bad payload", "message": str(exc)})
            return

        try:
            ctx.client.ingest_batch([event])
        except Exception as exc:
            self._send_json(502, {"ok": False, "error": "ingest_failed", "message": str(exc)})
            return

        ctx.bump(posted=1)
        self._send_json(200, {"posted": 1, "dropped": 0})


def make_server(*, host: str, port: int, context: ReceiverContext) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), AttentionHandler)
    server.context = context  # type: ignore[attr-defined]
    return server
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/relay.py tests/test_relay.py
git commit -m "feat(relay): POST /attention happy path on ThreadingHTTPServer"
```

---

## Task 9: relay.py — bearer-token authorization

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/relay.py`
- Modify: `/Users/Scanning/Developer/fulcra-attention/tests/test_relay.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/test_relay.py`:

```python
def test_post_missing_auth_returns_401(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/attention",
                 body=json.dumps({
                     "url": "https://x.com/", "title": "T",
                     "category": None,
                     "start_time": _now(), "end_time": _now(),
                     "client": "c"}),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 401
    body = json.loads(resp.read())
    assert body["error"] == "unauthorized"
    conn.close()


def test_post_wrong_bearer_returns_401(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": _now(), "end_time": _now(), "client": "c",
    }, token="not-the-token")
    assert status == 401
    assert payload["error"] == "unauthorized"


def test_get_health_does_not_require_auth(running_server):
    _server, port, _ctx = running_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    body = json.loads(resp.read())
    assert body["ok"] is True
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: 2 new test cases FAIL (status=200 instead of 401)

- [ ] **Step 3: Add bearer-token check to relay.py**

In `relay.py`, modify `do_POST` to call `_authorize` after the path check. Add the `_authorize` method:

```python
    def _authorize(self) -> bool:
        ctx = self._context()
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token != ctx.bearer_token:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False
        return True
```

And insert immediately after the `/attention` path check in `do_POST`:

```python
        if not self._authorize():
            return
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: All PASS (6 cases)

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/relay.py tests/test_relay.py
git commit -m "feat(relay): bearer-token auth on POST /attention (health endpoint stays open)"
```

---

## Task 10: relay.py — schema validation

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/relay.py`
- Modify: `/Users/Scanning/Developer/fulcra-attention/tests/test_relay.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/test_relay.py`:

```python
def test_post_both_url_and_category_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": "banking",
        "start_time": _now(), "end_time": _now(), "client": "c",
    })
    assert status == 400
    assert payload["error"] == "bad payload"
    assert "url" in payload["message"] and "category" in payload["message"]


def test_post_neither_url_nor_category_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {
        "url": None, "title": None, "category": None,
        "start_time": _now(), "end_time": _now(), "client": "c",
    })
    assert status == 400
    assert payload["error"] == "bad payload"


def test_post_end_before_start_rejected(running_server):
    _server, port, _ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": now.isoformat().replace("+00:00", "Z"),
        "end_time":   (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    assert status == 400


def test_post_future_end_time_rejected(running_server):
    _server, port, _ctx = running_server
    now = datetime.now(timezone.utc).replace(microsecond=0)
    status, payload = _post(port, {
        "url": "https://x.com/", "title": "T", "category": None,
        "start_time": now.isoformat().replace("+00:00", "Z"),
        "end_time":   (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "client": "c",
    })
    assert status == 400


def test_post_missing_required_fields_rejected(running_server):
    _server, port, _ctx = running_server
    status, payload = _post(port, {"url": "https://x.com/"})
    assert status == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: 5 new test cases FAIL (200 instead of 400, or 502 on missing fields)

- [ ] **Step 3: Add schema validation to relay.py**

Add a `_validate` function at module level:

```python
def _validate(payload: dict) -> None:
    """Raise ValueError with a human-readable message on schema violation."""
    required = ("start_time", "end_time", "client")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    url = payload.get("url")
    cat = payload.get("category")
    if (url is None) == (cat is None):
        raise ValueError("exactly one of {url, category} must be non-null")
    try:
        from .ingest import _parse_iso
        st = _parse_iso(payload["start_time"])
        en = _parse_iso(payload["end_time"])
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp: {exc}") from exc
    if st > en:
        raise ValueError("start_time > end_time")
    now = datetime.now(timezone.utc)
    if en > now + timedelta(minutes=5):
        raise ValueError("end_time more than 5 minutes in the future")
```

In `do_POST`, call `_validate(payload)` immediately after `json.loads`, and wrap the validation + event build in one try/except ValueError block returning 400 `{"ok": False, "error": "bad payload", "message": str(exc)}`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_relay.py -v
```
Expected: All PASS (11 cases)

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/relay.py tests/test_relay.py
git commit -m "feat(relay): schema validation (xor url/category, time ordering, clock-skew bound)"
```

---

## Task 11: service_manager.py — launchd plist

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/service_manager.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_service_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_service_manager.py
"""Service manager: launchd (macOS) + systemd user (Linux) generation."""
from __future__ import annotations

from pathlib import Path

import pytest

from fulcra_attention.service_manager import (
    LAUNCHD_LABEL,
    launchd_plist_path,
    render_launchd_plist,
    render_systemd_unit,
    systemd_unit_path,
)


def test_render_launchd_plist_contains_required_keys():
    plist = render_launchd_plist(executable="/opt/homebrew/bin/fulcra-attention")
    assert "<key>Label</key>" in plist
    assert f"<string>{LAUNCHD_LABEL}</string>" in plist
    assert "<key>ProgramArguments</key>" in plist
    assert "<string>/opt/homebrew/bin/fulcra-attention</string>" in plist
    assert "<string>relay</string>" in plist
    assert "<key>RunAtLoad</key>" in plist
    assert "<true/>" in plist
    assert "<key>KeepAlive</key>" in plist


def test_render_launchd_plist_uses_loopback_only_log_paths():
    plist = render_launchd_plist(executable="/usr/local/bin/fulcra-attention")
    assert "StandardOutPath" in plist
    assert "StandardErrorPath" in plist


def test_launchd_plist_path_under_launchagents():
    p = launchd_plist_path()
    assert "Library/LaunchAgents" in str(p)
    assert p.name == "com.fulcra.attention.relay.plist"


def test_render_systemd_unit_basic_structure():
    unit = render_systemd_unit(executable="/usr/local/bin/fulcra-attention")
    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "[Install]" in unit
    assert "ExecStart=/usr/local/bin/fulcra-attention relay" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_systemd_unit_path_under_user_systemd():
    p = systemd_unit_path()
    assert ".config/systemd/user" in str(p)
    assert p.name == "fulcra-attention-relay.service"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_service_manager.py -v
```
Expected: All FAIL (ModuleNotFoundError: fulcra_attention.service_manager)

- [ ] **Step 3: Implement service_manager.py**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/service_manager.py
"""Generate + install OS-level service definitions for the relay.

macOS: launchd user agent at ~/Library/LaunchAgents/com.fulcra.attention.relay.plist
Linux: systemd user unit at ~/.config/systemd/user/fulcra-attention-relay.service
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

LAUNCHD_LABEL = "com.fulcra.attention.relay"
SYSTEMD_NAME = "fulcra-attention-relay"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_NAME}.service"


def render_launchd_plist(*, executable: str) -> str:
    log_dir = Path.home() / "Library" / "Logs" / "fulcra-attention"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>relay</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/relay.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/relay.err.log</string>
</dict>
</plist>
"""


def render_systemd_unit(*, executable: str) -> str:
    return f"""[Unit]
Description=Fulcra Attention relay (loopback HTTP for the Chrome ext)
After=network.target

[Service]
Type=simple
ExecStart={executable} relay
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


def install(*, executable: str) -> Path:
    """Render and write the appropriate service file; return its path.

    macOS: writes the launchd plist (caller is expected to `launchctl load` it).
    Linux: writes the systemd user unit (caller: `systemctl --user enable --now`).
    """
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path()
        content = render_launchd_plist(executable=executable)
    elif system == "Linux":
        path = systemd_unit_path()
        content = render_systemd_unit(executable=executable)
    else:
        raise RuntimeError(f"unsupported platform: {system!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o644)
    return path
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_service_manager.py -v
```
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/service_manager.py tests/test_service_manager.py
git commit -m "feat(service): launchd + systemd user-unit generation for the relay"
```

---

## Task 12: cli.py — bootstrap subcommand

**Files:**
- Create: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/cli.py`
- Test:   `/Users/Scanning/Developer/fulcra-attention/tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# /Users/Scanning/Developer/fulcra-attention/tests/test_cli.py
"""CLI entry point — exercised via click's CliRunner."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from fulcra_attention import state as state_mod
from fulcra_attention.cli import cli


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "DEFAULT_PATH", state_path)
    monkeypatch.setenv("FULCRA_ATTENTION_STATE", str(state_path))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    yield state_path


def test_bootstrap_creates_def_and_tags(_isolate_state, mocker):
    posted: list[dict] = []
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            body = json.loads(r.content)
            return httpx.Response(200, json={"id": f"tag-{body['name']}"})
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-attention"})
        raise AssertionError(f"unexpected {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )

    res = CliRunner().invoke(cli, ["bootstrap"])
    assert res.exit_code == 0, res.output
    assert "def-attention" in res.output

    # State persisted
    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id == "def-attention"


def test_bootstrap_idempotent(_isolate_state, mocker):
    # Pre-populate state with definition; transport should never be hit.
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-existing",
            tag_ids={"attention": "a", "web": "w"},
        ),
        _isolate_state,
    )

    def responder(r: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no requests expected, got {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["bootstrap"])
    assert res.exit_code == 0
    assert "def-existing" in res.output
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 2 FAIL (ModuleNotFoundError: fulcra_attention.cli)

- [ ] **Step 3: Implement cli.py (bootstrap only)**

```python
# /Users/Scanning/Developer/fulcra-attention/fulcra_attention/cli.py
"""click CLI entry point."""
from __future__ import annotations

import click

from . import state as state_mod
from .fulcra import FulcraClient


@click.group(help="Capture browsing attention into Fulcra.")
def cli() -> None:
    pass


@cli.command(help="Create the Attention DurationAnnotation def + attention/web tags (idempotent).")
def bootstrap() -> None:
    s = state_mod.load()
    client = FulcraClient()
    client.ensure_definitions(s)
    state_mod.save(s)
    click.echo(f"attention={s.attention_definition_id}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/cli.py tests/test_cli.py
git commit -m "feat(cli): bootstrap subcommand (idempotent def+tag creation)"
```

---

## Task 13: cli.py — setup subcommand (bearer + service install)

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/cli.py`
- Modify: `/Users/Scanning/Developer/fulcra-attention/tests/test_cli.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/test_cli.py`:

```python
def test_setup_generates_bearer_token_and_relay_json(_isolate_state, tmp_path, mocker, monkeypatch):
    relay_dir = tmp_path / "fulcra-attention-config"
    monkeypatch.setenv("FULCRA_ATTENTION_RELAY_JSON", str(relay_dir / "relay.json"))
    # Skip service install on the test box.
    fake_install = mocker.patch(
        "fulcra_attention.cli.service_manager.install",
        return_value=tmp_path / "fake-service-file",
    )
    res = CliRunner().invoke(cli, ["setup"])
    assert res.exit_code == 0, res.output
    relay_json = relay_dir / "relay.json"
    assert relay_json.exists()
    body = json.loads(relay_json.read_text())
    assert "bearer_token" in body and len(body["bearer_token"]) >= 40
    assert body["port"] == 8771
    # Token printed for paste-into-extension
    assert body["bearer_token"] in res.output
    fake_install.assert_called_once()


def test_setup_is_idempotent_preserves_existing_token(_isolate_state, tmp_path, mocker, monkeypatch):
    relay_json = tmp_path / "relay.json"
    relay_json.write_text(json.dumps({"bearer_token": "PRE-EXISTING", "port": 8771}))
    monkeypatch.setenv("FULCRA_ATTENTION_RELAY_JSON", str(relay_json))
    mocker.patch("fulcra_attention.cli.service_manager.install",
                 return_value=tmp_path / "fake")
    res = CliRunner().invoke(cli, ["setup"])
    assert res.exit_code == 0
    body = json.loads(relay_json.read_text())
    assert body["bearer_token"] == "PRE-EXISTING"
    assert "PRE-EXISTING" in res.output
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 2 new FAIL (No such command 'setup')

- [ ] **Step 3: Add setup command + relay-json helper to cli.py**

Insert after the `bootstrap` command in `cli.py`:

```python
import json as _json
import os as _os
import secrets as _secrets
import shutil as _shutil
import stat as _stat
from pathlib import Path as _Path

from . import service_manager


def _relay_json_path() -> _Path:
    return _Path(
        _os.environ.get("FULCRA_ATTENTION_RELAY_JSON")
        or _os.path.expanduser("~/.config/fulcra-attention/relay.json")
    )


def _load_or_create_relay_json(port: int = 8771) -> dict:
    path = _relay_json_path()
    if path.exists():
        return _json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"bearer_token": _secrets.token_urlsafe(32), "port": port}
    path.write_text(_json.dumps(body, indent=2, sort_keys=True))
    _os.chmod(path, _stat.S_IRUSR | _stat.S_IWUSR)  # 0600
    return body


@cli.command(help="Generate bearer token and install the relay as a system service.")
def setup() -> None:
    relay = _load_or_create_relay_json()
    exe = _shutil.which("fulcra-attention") or "fulcra-attention"
    path = service_manager.install(executable=exe)
    click.echo(f"Bearer token: {relay['bearer_token']}")
    click.echo(f"Port:         {relay['port']}")
    click.echo(f"Service file: {path}")
    click.echo()
    click.echo("Paste the bearer token into the Chrome extension popup.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: All PASS (4 cases)

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/cli.py tests/test_cli.py
git commit -m "feat(cli): setup subcommand (generate bearer token, install service file)"
```

---

## Task 14: cli.py — status, reset, relay foreground

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/fulcra_attention/cli.py`
- Modify: `/Users/Scanning/Developer/fulcra-attention/tests/test_cli.py`

- [ ] **Step 1: Append failing tests**

Add to `tests/test_cli.py`:

```python
def test_status_prints_state_json(_isolate_state):
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-x",
            tag_ids={"attention": "a", "web": "w"},
            watermarks={"curl/0.1": "2026-05-18T14:00:00Z"},
        ),
        _isolate_state,
    )
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert parsed["attention_definition_id"] == "def-x"
    assert parsed["tag_ids"]["attention"] == "a"
    assert parsed["watermarks"]["curl/0.1"] == "2026-05-18T14:00:00Z"


def test_reset_requires_confirm(_isolate_state):
    state_mod.save(
        state_mod.State(attention_definition_id="def-x"),
        _isolate_state,
    )
    res = CliRunner().invoke(cli, ["reset"])
    assert res.exit_code != 0  # UsageError without --confirm
    assert "--confirm" in res.output


def test_reset_with_confirm_soft_deletes_and_clears(_isolate_state, mocker):
    state_mod.save(
        state_mod.State(
            attention_definition_id="def-to-delete",
            tag_ids={"attention": "a", "web": "w"},
            watermarks={"curl": "2026-05-18T14:00:00Z"},
        ),
        _isolate_state,
    )
    calls = []
    def responder(r: httpx.Request) -> httpx.Response:
        calls.append((r.method, r.url.path))
        if r.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected {r.method} {r.url}")
    transport = httpx.MockTransport(responder)
    mocker.patch(
        "fulcra_attention.cli.FulcraClient",
        lambda **kw: __import__("fulcra_attention.fulcra", fromlist=["FulcraClient"]).FulcraClient(transport=transport, **kw),
    )
    res = CliRunner().invoke(cli, ["reset", "--confirm"])
    assert res.exit_code == 0
    assert ("DELETE", "/user/v1alpha1/annotation/def-to-delete") in calls
    s = state_mod.load(_isolate_state)
    assert s.attention_definition_id is None
    assert s.watermarks == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: 3 new FAIL

- [ ] **Step 3: Add status/reset/relay to cli.py and soft_delete to FulcraClient**

First add to `fulcra.py`:

```python
    def soft_delete_definition(self, definition_id: str) -> bool:
        r = self._client().delete(
            f"/user/v1alpha1/annotation/{definition_id}",
            headers=self._authed_headers(),
        )
        if r.status_code == 204:
            return True
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return False
```

Then append to `cli.py`:

```python
@cli.command(help="Print the cached state.json contents.")
def status() -> None:
    s = state_mod.load()
    click.echo(_json.dumps(
        {
            "attention_definition_id": s.attention_definition_id,
            "tag_ids": s.tag_ids,
            "watermarks": s.watermarks,
        },
        indent=2, sort_keys=True,
    ))


@cli.command(help="Soft-delete the Attention def + clear local state.")
@click.option("--confirm", is_flag=True,
              help="Required. Confirms you understand orphaned events stay visible.")
def reset(confirm: bool) -> None:
    if not confirm:
        raise click.UsageError(
            "Pass --confirm. This soft-deletes the Attention definition; "
            "previously-ingested events stay visible (Fulcra has no per-event delete)."
        )
    s = state_mod.load()
    client = FulcraClient()
    if s.attention_definition_id:
        client.soft_delete_definition(s.attention_definition_id)
        click.echo(f"soft-deleted: {s.attention_definition_id}")
        s.attention_definition_id = None
    s.watermarks = {}
    state_mod.save(s)


@cli.command(help="Foreground-run the relay (intended for launchd/systemd).")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=None, type=int,
              help="Override port from ~/.config/fulcra-attention/relay.json.")
def relay(host: str, port: int | None) -> None:
    from .relay import ReceiverContext, make_server
    cfg = _load_or_create_relay_json()
    actual_port = port or cfg["port"]
    s = state_mod.load()
    if not s.attention_definition_id:
        raise click.ClickException("run `fulcra-attention bootstrap` first")
    client = FulcraClient()
    ctx = ReceiverContext(client=client, state=s, bearer_token=cfg["bearer_token"])
    server = make_server(host=host, port=actual_port, context=ctx)
    click.echo(f"listening on http://{host}:{actual_port}/attention")
    server.serve_forever()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_cli.py -v
```
Expected: All PASS (7 cases)

- [ ] **Step 5: Commit**

```bash
git add fulcra_attention/cli.py fulcra_attention/fulcra.py tests/test_cli.py
git commit -m "feat(cli): status + reset (with --confirm) + relay foreground command"
```

---

## Task 15: Full-suite green + flesh out README

**Files:**
- Modify: `/Users/Scanning/Developer/fulcra-attention/README.md`

- [ ] **Step 1: Run the full suite to confirm everything's green together**

```bash
cd /Users/Scanning/Developer/fulcra-attention
.venv/bin/pytest -q
```
Expected: All tests PASS (cumulative count ~35-40 across all test_*.py files)

- [ ] **Step 2: Verify CLI lists all subcommands**

```bash
.venv/bin/fulcra-attention --help
```
Expected: lists `bootstrap`, `setup`, `status`, `reset`, `relay`

- [ ] **Step 3: Flesh out README.md**

```markdown
# /Users/Scanning/Developer/fulcra-attention/README.md
# fulcra-attention

Capture what takes your attention while browsing — every page you read, with title and time-on-page — into your own [Fulcra](https://fulcradynamics.com) account.

This repo is the **Python relay + CLI** that the Chrome extension talks to. The extension itself is in Plan B (separate work item).

## Per-machine install

```bash
# 1. Install
pipx install fulcra-attention             # or: pip install -e . from a clone

# 2. Authenticate to Fulcra (OIDC device flow; uses the existing fulcra-api CLI)
fulcra auth login

# 3. Bootstrap the Attention definition + tags (idempotent)
fulcra-attention bootstrap

# 4. Generate the bearer token + install launchd/systemd service
fulcra-attention setup

# (Paste the printed bearer token into the Chrome extension's popup later.)
```

## Architecture

- The relay binds to `127.0.0.1:8771` (loopback only — no LAN exposure in v1).
- One endpoint: `POST /attention`. Bearer-token-authenticated.
- Payload: `{url|category, title, og_description, favicon_url, start_time, end_time, client}` — exactly one of `url` or `category` non-null.
- Each accepted ping becomes one `DurationAnnotation` under the `Attention` def, tagged `attention` + `web`.
- Source-id idempotency: `com.fulcra.attention.v1.<sha256(url_or_category|start_time_to_second)[:16]>` — re-posts are silent no-ops.

See `docs/superpowers/specs/2026-05-18-fulcra-attention-v1-design.md` (mirrored from FulcraMediaHelpers).

## Manual smoke test

After `bootstrap` and `setup`, with the service running:

```bash
TOKEN=$(jq -r .bearer_token ~/.config/fulcra-attention/relay.json)
curl -X POST http://127.0.0.1:8771/attention \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://example.com/article\",\"title\":\"Smoke test\",\"category\":null,\"start_time\":\"$(date -u +%FT%TZ -d '5 min ago' 2>/dev/null || date -u -v-5M +%FT%TZ)\",\"end_time\":\"$(date -u +%FT%TZ)\",\"client\":\"curl/0.1\"}"
```

Expected: `{"posted":1,"dropped":0}`. Confirm the annotation appears in your Fulcra account via:

```bash
fulcra get-records --type DurationAnnotation --start "1 hour ago" | jq '.[] | select(.data.service == "web")'
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

## Status

- Plan A (this repo): Python backend complete.
- Plan B: Chrome extension (separate work item).
- v2: Direct-to-cloud via Auth0 OAuth (extension drops the relay dependency).
```

- [ ] **Step 4: Commit + push**

```bash
git add README.md
git commit -m "docs: README with install + manual smoke test"
```

- [ ] **Step 5: Create the GitHub repo + push**

```bash
# Use the gh CLI to create the repo (private, under ashfulcra)
gh repo create ashfulcra/fulcra-attention --private --source . --remote origin --description "Capture browsing attention into Fulcra (Python relay + CLI; Chrome ext in Plan B)"
git push -u origin main
```

Expected output: GitHub URL `https://github.com/ashfulcra/fulcra-attention`. Repo is private.

---

## Task 16: Live integration smoke test (manual, documented)

This is **not a code task** — it's a hand-on validation gate at the end of Plan A. Run it once to confirm the full backend works against real Fulcra.

- [ ] **Step 1: Verify `fulcra auth login` is current**

```bash
fulcra auth print-access-token | head -c 20 && echo "..."
```
Expected: a JWT prefix (no error). Otherwise: `fulcra auth login` first.

- [ ] **Step 2: Bootstrap against real Fulcra**

```bash
fulcra-attention bootstrap
```
Expected: `attention=<real-uuid>`. Re-run; second run should print the same UUID without making any API calls (idempotent).

- [ ] **Step 3: `setup` + start the service**

```bash
fulcra-attention setup
# Read the bearer token and service-file path it prints.

# macOS:
launchctl load ~/Library/LaunchAgents/com.fulcra.attention.relay.plist
# OR Linux:
systemctl --user daemon-reload && systemctl --user enable --now fulcra-attention-relay
```

- [ ] **Step 4: Health check**

```bash
curl http://127.0.0.1:8771/health
```
Expected: `{"ok":true,"definition_id":"<uuid>",...}`.

- [ ] **Step 5: POST a fake attention ping**

```bash
TOKEN=$(jq -r .bearer_token ~/.config/fulcra-attention/relay.json)
NOW=$(date -u +%FT%TZ)
FIVE_MIN_AGO=$(date -u -v-5M +%FT%TZ 2>/dev/null || date -u -d '5 min ago' +%FT%TZ)
curl -X POST http://127.0.0.1:8771/attention \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"https://example.com/article\",\"title\":\"Manual smoke test\",\"category\":null,\"start_time\":\"$FIVE_MIN_AGO\",\"end_time\":\"$NOW\",\"client\":\"curl/0.1\"}"
```
Expected: HTTP 200, `{"posted":1,"dropped":0}`.

- [ ] **Step 6: Verify the annotation landed in Fulcra**

```bash
fulcra get-records --type DurationAnnotation --start "10 minutes ago" \
  | jq '.[] | select(.data.service == "web") | {url: .data.url, title: .data.title, source: .metadata.source}'
```
Expected: one record with `url == "https://example.com/article"`, `title == "Manual smoke test"`, and source-id `com.fulcra.attention.v1.<16hex>`.

- [ ] **Step 7: Idempotency check — replay**

Re-run the same `curl` from Step 5. Then re-run Step 6. Expected: still exactly one record (Fulcra's source-id idempotency makes the replay a no-op).

- [ ] **Step 8: Note the result in the commit log (optional)**

```bash
git commit --allow-empty -m "smoke: Plan A end-to-end validated against live Fulcra"
```

End of Plan A. Plan B (Chrome extension) builds against this same `POST /attention` contract.
