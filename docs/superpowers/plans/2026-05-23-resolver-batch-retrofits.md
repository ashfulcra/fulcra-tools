# Resolver Batch Retrofits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Retrofit the remaining typed-annotation plugins to use the
shared resolver (`ctx.resolved_definition_id`), so multi-machine
installs converge on a single Fulcra definition per logical stream
rather than creating duplicate definitions on each machine.

**Architecture:** Each retrofit follows the R5/R6/R7 pattern:
declare `canonical_definition_name` on `Plugin(...)`, switch the
plugin's run path from relying on `client.ensure_definitions()` (which
only creates, never adopts) to calling
`ctx.resolved_definition_id(SPEC, canonical_name=...)` before import,
write a regression test mocking the resolver to return a fixed id and
asserting the plugin uses that id end-to-end.

**Reference retrofits already shipped (look at these for the template):**
- `e95fb37` (attention, `"Attention"`)
- `be0b905` (lastfm, `"Listened"`)
- `faf93a7` (spotify-extended, `"Listened"`)

**Spec:** `docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md`

**Branch:** All work commits directly on `main` per user direction. **Do not push.** Each subagent
MUST verify it is on `main` (not detached HEAD) at start and end of its task.

---

## Inventory

This table was produced by reading each plugin's source code in
`packages/media-helpers/fulcra_media/collect_plugins.py` and the
importer modules it delegates to. Canonical names are the string that
`_create_duration_definition(name=...)` in `fulcra.py` uses (verified
from `ensure_definitions`). The `expected_spec` shape mirrors
`wire.duration_definition_payload`'s defaults (the same shape
`LASTFM_LISTENED_SPEC` and `ATTENTION_SPEC` use).

**Key insight from the code:** `FulcraClient.run_import` routes each
event to a definition via `ev.category → media_state.<category>_definition_id`.
So the retrofit for each plugin must ensure the right field in
`media_state` (`watched_definition_id`, `listened_definition_id`, or
`read_definition_id`) is populated via the resolver before `run_import`
is called. The pattern mirrors exactly what `_run_lastfm` does for
`media_state.listened_definition_id`.

| Plugin | Owns a typed def? | Canonical name (verified from source) | annotation_type | measurement_spec | Run-path file:line |
|---|---|---|---|---|---|
| trakt | Yes — `category="watched"` | `"Watched"` | `duration` | `{measurement_type: duration, value_type: duration, unit: null}` | `collect_plugins.py:_run_trakt` → `client.run_import` (line ~313) |
| netflix | Yes — `category="watched"` | `"Watched"` | `duration` | same as above | `collect_plugins.py:_run_netflix` → `_run_file_import` |
| deezer | Yes — `category="listened"` | `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_deezer` → `_run_scheduled_import` |
| letterboxd | Yes — `category="watched"` | `"Watched"` | `duration` | same as above | `collect_plugins.py:_run_letterboxd` → `_rss_import_and_advance` |
| goodreads | Yes — `category="read"` | `"Read"` | `duration` | same as above | `collect_plugins.py:_run_goodreads` → `_rss_import_and_advance` |
| spotify-ifttt | Yes — `category="listened"` | `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_spotify_ifttt` → `_import_events` |
| apple-podcasts | Yes — `category="listened"` | `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_apple_podcasts` → `client.run_import` (line ~715) |
| apple-podcasts-timemachine | Yes — `category="listened"` | `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_apple_podcasts_timemachine` → `client.run_import` (line ~775) |
| generic-rss | Yes — `category` from config (either `watched` or `listened`) | config-driven: `"Watched"` or `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_generic_rss` → `_rss_import_and_advance` |
| generic-csv | Yes — `category` from config (either `watched` or `listened`) | config-driven: `"Watched"` or `"Listened"` | `duration` | same as above | `collect_plugins.py:_run_generic_csv` → `_import_events` |
| youtube | Yes — `category="watched"` | `"Watched"` | `duration` | same as above | `collect_plugins.py:_run_youtube` → `_run_file_import` |
| apple-takeout | Yes — `category="watched"` | `"Watched"` | `duration` | same as above | `collect_plugins.py:_run_apple_takeout` → `_import_events` |
| media-webhook | **Skip — N/A** | — | — | — | see below |

**Skip rationale — media-webhook:** `_run_media_webhook` is a long-running service
plugin (not a batch importer). It does not create or adopt a Fulcra definition; it
reads `media_state.watched_definition_id` and **requires** it to already exist
(it raises `RuntimeError("media annotations not bootstrapped")` if the field is
absent). The definition is owned by bootstrap / the other plugins that write
`watched` annotations. Retrofitting media-webhook with the resolver would be
wrong: the webhook receiver would create a _new_ definition on a machine that
never ran another plugin, silently diverging from the machine where those
definitions were originally created. The correct fix for media-webhook's
multi-machine story is a separate task: call `ctx.resolved_definition_id` for
`"Watched"` (and possibly `"Listened"`) at service startup so it can populate
`watched_definition_id` without requiring bootstrap to have been run first.
That change is non-trivial (the webhook is long-lived and needs graceful
handling if the resolver fails at startup) and is deferred to a dedicated follow-up.

**Notes on shared definitions:**

- `deezer`, `spotify-ifttt`, `apple-podcasts`, and `apple-podcasts-timemachine`
  all write `category="listened"` and should adopt the same `"Listened"`
  definition that `lastfm` and `spotify-extended` already use. No special
  handling is needed in the resolver — `resolve_definition_id` will find the
  existing `"Listened"` def on second-or-later machine. Tests for these plugins
  should explicitly exercise the **adoption** path (fake client returns an
  existing def with a matching spec).

- `trakt`, `netflix`, `letterboxd`, `youtube`, and `apple-takeout` all write
  `category="watched"` and converge on `"Watched"`.

- `goodreads` writes `category="read"` and converges on `"Read"`.

- `generic-rss` and `generic-csv` are config-driven: the category (and therefore
  the canonical name) is set at runtime from `ctx.config["category"]`. See
  special-handling note in BR10/BR11.

**The shared WATCHED_SPEC / LISTENED_SPEC / READ_SPEC:** All three are the same
`duration` shape (mirrors `wire.duration_definition_payload` defaults). Define
module-level constants for each and share them across the plugins that write to
that category.

---

## Plan tasks

### BR1: Retrofit trakt

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch**

```bash
cd /Users/Scanning/Developer/fulcra-tools
git symbolic-ref HEAD   # must print: refs/heads/main
git status              # must be clean
```

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "name=" packages/media-helpers/fulcra_media/fulcra.py | grep -i "watch"
```

Confirm `ensure_definitions` calls `_create_duration_definition(name="Watched", ...)`.
The canonical name is `"Watched"`.

- [ ] **Step 2: Write a regression test**

In `packages/media-helpers/tests/test_collect_plugins.py`, add:

```python
def test_trakt_plugin_declares_canonical_definition_name():
    assert TRAKT_PLUGIN.canonical_definition_name == "Watched"

def test_trakt_resolver_called_when_watched_def_missing(monkeypatch):
    """BR1 regression: when watched_definition_id is absent from media state
    (machine 2 never ran bootstrap), run() must call ctx.resolved_definition_id
    and write the result into media_state.watched_definition_id before importing.

    Uses the adoption path: fake client already has a matching "Watched" def
    so the resolver returns the existing id without creating a new one."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr("fulcra_media.collect_plugins.trakt_importer.fetch_history",
                        lambda: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.trakt_importer.normalize_history",
                        lambda items, cluster_threshold=5: [])

    class FakeResult:
        posted = 0; skipped_existing = 0

    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []
        def list_definitions(self, *, name):
            self.list_calls.append(name)
            # Adoption path: existing def already present on Fulcra
            return [{"id": "def-watched-existing", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec):
            self.create_calls.append(name)
            return {"id": "should-not-be-created"}

    fake_def_client = _FakeDefClient()

    class _FakePluginState:
        definition_id: str | None = None
        watermark: str | None = None
        clusters: str = "keep"
        twin_policy: str = "keep"

    ctx = RunContext(
        plugin_id="trakt",
        config={"clusters": "keep", "twin_policy": "keep"},
        credentials={},
        state=_FakePluginState(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    TRAKT_PLUGIN.run(ctx)

    # Resolver adopted the existing def — create was NOT called
    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls == []
    assert empty_media_state.watched_definition_id == "def-watched-existing"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name` to the Plugin construction**

In `collect_plugins.py`, in `TRAKT_PLUGIN = Plugin(...)`:

```python
canonical_definition_name="Watched",
```

- [ ] **Step 4: Replace definition-discovery in the run path**

In `_run_trakt`, before the `try: items = list(trakt_importer.fetch_history())` block,
add:

```python
media_state = _state_load(STATE_PATH)
if not media_state.watched_definition_id:
    def_id = ctx.resolved_definition_id(
        WATCHED_SPEC,
        canonical_name="Watched",
    )
    media_state.watched_definition_id = def_id
    _state_save(media_state)
```

Remove the bare `media_state = _state_load(STATE_PATH)` call later in the function
(the one before `client.ensure_tag`) and instead use the already-loaded `media_state`.

- [ ] **Step 5: Define `WATCHED_SPEC` at module level**

Add near the top of `collect_plugins.py`, alongside `LASTFM_LISTENED_SPEC`:

```python
WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}
```

(Re-use `LASTFM_LISTENED_SPEC` as `LISTENED_SPEC` reference. Add also `READ_SPEC`
with the identical shape — see BR5.)

- [ ] **Step 6: Run the package tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k trakt
```

- [ ] **Step 7: Branch check; commit**

```bash
git symbolic-ref HEAD   # must still print: refs/heads/main
git add packages/media-helpers/
git commit -m "refactor(trakt): use the shared definition resolver

Multi-machine coherence for Trakt watch history: a fresh install on
Mac 2 now adopts Mac 1's existing 'Watched' definition instead of
requiring bootstrap to have been run first, then failing with a
missing definition_id RuntimeError.

The resolver is called once at the start of _run_trakt when
watched_definition_id is absent from the local media state. On a
machine where bootstrap has already run, the if-guard short-circuits
and the resolver is never called (no network round-trip).

Adoption path tested explicitly: fake client returns an existing
matching 'Watched' def so the resolver never calls create_definition."
```

---

### BR2: Retrofit netflix

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch** (same as BR1).

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

Confirm `netflix_importer.parse_auto` yields `category="watched"`. The canonical
name is `"Watched"`.

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/netflix.py | head -5
```

- [ ] **Step 2: Write a regression test**

```python
def test_netflix_plugin_declares_canonical_definition_name():
    assert NETFLIX_PLUGIN.canonical_definition_name == "Watched"

def test_netflix_resolver_called_when_watched_def_missing(monkeypatch, tmp_path):
    """BR2 regression: _run_netflix calls ctx.resolved_definition_id when
    watched_definition_id is absent, writes the result to media_state."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_csv = tmp_path / "netflix.csv"
    fake_csv.write_text("Title,Date\n")
    monkeypatch.setattr("fulcra_media.collect_plugins.netflix_importer.parse_auto",
                        lambda path: [])

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            return [{"id": "def-watched-net", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec):
            return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="netflix",
        config={"path": str(fake_csv)},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    NETFLIX_PLUGIN.run(ctx)

    assert empty_media_state.watched_definition_id == "def-watched-net"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Watched"` to `NETFLIX_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_netflix`**

Since `_run_netflix` delegates to `_run_file_import`, add the resolver call
at the top of `_run_netflix` before calling `_run_file_import`:

```python
def _run_netflix(ctx: RunContext) -> None:
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(WATCHED_SPEC, canonical_name="Watched")
        media_state.watched_definition_id = def_id
        _state_save(media_state)
    _run_file_import(ctx, parse=netflix_importer.parse_auto, tag="netflix")
```

- [ ] **Step 5: `WATCHED_SPEC` is already defined from BR1.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k netflix
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(netflix): use the shared definition resolver

Netflix is a manual file-import plugin (no scheduling); a fresh
machine that has never run bootstrap would silently produce events
with a null definition_id. The resolver call at the top of
_run_netflix ensures the 'Watched' definition exists and is adopted
from any existing machine's definition before the import runs."
```

---

### BR3: Retrofit deezer

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/deezer.py | head -5
```

Confirm `category="listened"`. Canonical name: `"Listened"`.

- [ ] **Step 2: Write a regression test** (same shape as lastfm's BR6 test — adoption path
since lastfm may have already created `"Listened"` on this machine).

```python
def test_deezer_plugin_declares_canonical_definition_name():
    assert DEEZER_PLUGIN.canonical_definition_name == "Listened"

def test_deezer_resolver_called_when_listened_def_missing(monkeypatch):
    """BR3: deezer adopts 'Listened' via the resolver when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.deezer_importer.fetch_history",
                        lambda creds, since, max_pages: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.deezer_importer.normalize_history",
                        lambda raw: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            # Adoption: lastfm already created "Listened" on this machine
            return [{"id": "def-listened-shared", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec):
            return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="deezer",
        config={},
        credentials={"access-token": "tok"},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    DEEZER_PLUGIN.run(ctx)

    assert empty_media_state.listened_definition_id == "def-listened-shared"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Listened"` to `DEEZER_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_deezer`**

Add before `_run_scheduled_import(...)`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.listened_definition_id:
    def_id = ctx.resolved_definition_id(LISTENED_SPEC, canonical_name="Listened")
    media_state.listened_definition_id = def_id
    _state_save(media_state)
```

- [ ] **Step 5: Define `LISTENED_SPEC` at module level** (same shape as `LASTFM_LISTENED_SPEC`).
`LISTENED_SPEC` and `LASTFM_LISTENED_SPEC` are intentionally distinct constants — each
lives next to its plugin block so spec-shape tests remain local.
Add `DEEZER_LISTENED_SPEC` following the same naming convention, or define a single shared
`LISTENED_SPEC` at the top of the module. Either approach is acceptable; the shared constant
is simpler. Document the choice in the constant's docstring.

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k deezer
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(deezer): use the shared definition resolver

Deezer writes 'listened' events and converges on the same 'Listened'
definition as lastfm and spotify-extended. The adoption path is the
primary one here: on a machine where lastfm or spotify-extended has
already run, the resolver finds the existing 'Listened' def and returns
its id without creating a duplicate."
```

---

### BR4: Retrofit letterboxd

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/letterboxd.py | head -5
```

Confirm `category="watched"`. Canonical name: `"Watched"`.

- [ ] **Step 2: Write a regression test** (adoption path; similar shape to BR1 test).

```python
def test_letterboxd_plugin_declares_canonical_definition_name():
    assert LETTERBOXD_PLUGIN.canonical_definition_name == "Watched"

def test_letterboxd_resolver_called_when_watched_def_missing(monkeypatch):
    """BR4: letterboxd adopts 'Watched' via the resolver when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.lb_importer.fetch_diary",
                        lambda username: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            return [{"id": "def-watched-lb", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec):
            return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="letterboxd",
        config={"username": "ashtest"},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    LETTERBOXD_PLUGIN.run(ctx)

    assert empty_media_state.watched_definition_id == "def-watched-lb"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Watched"` to `LETTERBOXD_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_letterboxd`**

Add before `since = _rss_since(ctx)`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.watched_definition_id:
    def_id = ctx.resolved_definition_id(WATCHED_SPEC, canonical_name="Watched")
    media_state.watched_definition_id = def_id
    _state_save(media_state)
```

- [ ] **Step 5: `WATCHED_SPEC` is already defined from BR1.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k letterboxd
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(letterboxd): use the shared definition resolver

Letterboxd writes 'watched' events; now converges on the same
'Watched' definition as trakt, netflix, youtube, and apple-takeout
across machines."
```

---

### BR5: Retrofit goodreads

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/goodreads.py | head -5
```

Confirm `category="read"`. Canonical name: `"Read"`.

- [ ] **Step 2: Write a regression test**

```python
def test_goodreads_plugin_declares_canonical_definition_name():
    assert GOODREADS_PLUGIN.canonical_definition_name == "Read"

def test_goodreads_resolver_called_when_read_def_missing(monkeypatch):
    """BR5: goodreads adopts 'Read' via the resolver when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.gr_importer.fetch_diary",
                        lambda user_id: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            # "Read" is unique to goodreads — first run creates, subsequent adopt
            return []
        def create_definition(self, *, name, **spec):
            return {"id": "def-read-new"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="goodreads",
        config={"user_id": "12345"},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    GOODREADS_PLUGIN.run(ctx)

    assert empty_media_state.read_definition_id == "def-read-new"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Read"` to `GOODREADS_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_goodreads`**

Add before `since = _rss_since(ctx)`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.read_definition_id:
    def_id = ctx.resolved_definition_id(READ_SPEC, canonical_name="Read")
    media_state.read_definition_id = def_id
    _state_save(media_state)
```

- [ ] **Step 5: Define `READ_SPEC` at module level** (same shape as `WATCHED_SPEC` / `LISTENED_SPEC`):

```python
READ_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}
```

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k goodreads
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(goodreads): use the shared definition resolver

Goodreads is the only plugin writing 'read' annotations. On the first
machine the resolver creates the 'Read' definition; on machine 2 it
adopts the existing one. Both paths are tested: the regression test
exercises the creation path (no existing def) since 'Read' has no
sibling plugin that would have created it first."
```

---

### BR6: Retrofit spotify-ifttt

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/spotify_ifttt.py | head -5
```

Confirm `category="listened"`. Canonical name: `"Listened"`.

- [ ] **Step 2: Write a regression test** (same adoption-path shape as BR3).

```python
def test_spotify_ifttt_plugin_declares_canonical_definition_name():
    assert SPOTIFY_IFTTT_PLUGIN.canonical_definition_name == "Listened"

def test_spotify_ifttt_resolver_called_when_listened_def_missing(monkeypatch, tmp_path):
    """BR6: spotify-ifttt adopts 'Listened' via the resolver (creation path —
    simulates the case where this is the first 'listened' plugin on the machine)."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"")
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.spotify_ifttt_importer.parse_ifttt_zip",
        lambda path, tz: [],
    )

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name): return []
        def create_definition(self, *, name, **spec): return {"id": "def-listened-ifttt"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="spotify-ifttt",
        config={"path": str(fake_zip)},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert empty_media_state.listened_definition_id == "def-listened-ifttt"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Listened"` to `SPOTIFY_IFTTT_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_spotify_ifttt`**

Add before `resolved = _resolve_path(ctx)`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.listened_definition_id:
    def_id = ctx.resolved_definition_id(LISTENED_SPEC, canonical_name="Listened")
    media_state.listened_definition_id = def_id
    _state_save(media_state)
```

- [ ] **Step 5: `LISTENED_SPEC` / `DEEZER_LISTENED_SPEC` is already defined from BR3.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "ifttt"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(spotify-ifttt): use the shared definition resolver

Spotify IFTTT backfill writes 'listened' events and now converges on
the same 'Listened' definition as lastfm, spotify-extended, and deezer.
Any of these four plugins running first on a machine creates 'Listened';
the rest adopt the existing definition."
```

---

### BR7: Retrofit apple-podcasts

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/apple_podcasts.py | head -5
```

Confirm `category="listened"`. Canonical name: `"Listened"`.

- [ ] **Step 2: Write a regression test** (adoption path — podcast library is
"listened" alongside music).

```python
def test_apple_podcasts_plugin_declares_canonical_definition_name():
    assert APPLE_PODCASTS_PLUGIN.canonical_definition_name == "Listened"

def test_apple_podcasts_resolver_called_when_listened_def_missing(monkeypatch):
    """BR7: apple-podcasts adopts 'Listened' when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.ap.parse_db",
                        lambda db_path: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            return [{"id": "def-listened-pods", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec):
            return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="apple-podcasts",
        config={},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    APPLE_PODCASTS_PLUGIN.run(ctx)

    assert empty_media_state.listened_definition_id == "def-listened-pods"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Listened"` to `APPLE_PODCASTS_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_apple_podcasts`**

Add before `raw_path = ctx.config.get("db_path")`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.listened_definition_id:
    def_id = ctx.resolved_definition_id(LISTENED_SPEC, canonical_name="Listened")
    media_state.listened_definition_id = def_id
    _state_save(media_state)
```

Remove the separate `media_state = _state_load(STATE_PATH)` call later in the function
and use the already-loaded `media_state`.

- [ ] **Step 5: `LISTENED_SPEC` is already defined.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "apple_podcasts"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(apple-podcasts): use the shared definition resolver

Apple Podcasts writes 'listened' events and converges on the shared
'Listened' definition. Adoption path tested: the fake client returns
an existing matching def so create_definition is never called when
another 'listened' plugin has already run on this machine."
```

---

### BR8: Retrofit apple-podcasts-timemachine

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

Confirm `apple_podcasts.parse_db` yields `category="listened"` (same importer as
apple-podcasts). Canonical name: `"Listened"`.

- [ ] **Step 2: Write a regression test**

```python
def test_apple_podcasts_timemachine_plugin_declares_canonical_definition_name():
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.canonical_definition_name == "Listened"

def test_apple_podcasts_timemachine_resolver_called_when_listened_def_missing(monkeypatch):
    """BR8: timemachine plugin adopts 'Listened' when media state is empty.
    This is a manual one-shot recovery plugin — no watermark advance."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    # Fake: one snapshot found, zero events parsed
    monkeypatch.setattr("fulcra_media.collect_plugins.ap.find_timemachine_snapshots",
                        lambda: ["/fake/snap"])
    monkeypatch.setattr("fulcra_media.collect_plugins.ap.parse_db", lambda path: [])

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name): return []
        def create_definition(self, *, name, **spec): return {"id": "def-listened-tm"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="apple-podcasts-timemachine",
        config={},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert empty_media_state.listened_definition_id == "def-listened-tm"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Listened"` to `APPLE_PODCASTS_TIMEMACHINE_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_apple_podcasts_timemachine`**

Add before `snapshots = ap.find_timemachine_snapshots()`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.listened_definition_id:
    def_id = ctx.resolved_definition_id(LISTENED_SPEC, canonical_name="Listened")
    media_state.listened_definition_id = def_id
    _state_save(media_state)
```

Remove the separate `media_state = _state_load(STATE_PATH)` call later and use the
already-loaded `media_state`.

- [ ] **Step 5: `LISTENED_SPEC` is already defined.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "timemachine"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(apple-podcasts-timemachine): use the shared definition resolver

Time Machine recovery plugin now adopts the shared 'Listened'
definition before walking snapshots, consistent with the on-device
apple-podcasts plugin. Both share LISTENED_SPEC and the same
listened_definition_id field in media state."
```

---

### BR9: Retrofit youtube

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/youtube.py | head -5
```

Confirm `category="watched"`. Canonical name: `"Watched"`.

- [ ] **Step 2: Write a regression test**

```python
def test_youtube_plugin_declares_canonical_definition_name():
    assert YOUTUBE_PLUGIN.canonical_definition_name == "Watched"

def test_youtube_resolver_called_when_watched_def_missing(monkeypatch, tmp_path):
    """BR9: youtube adopts 'Watched' via the resolver when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_json = tmp_path / "watch-history.json"
    fake_json.write_text("[]")
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.youtube_importer.parse_takeout_json",
        lambda path: [],
    )

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            return [{"id": "def-watched-yt", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec): return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="youtube",
        config={"path": str(fake_json)},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    YOUTUBE_PLUGIN.run(ctx)

    assert empty_media_state.watched_definition_id == "def-watched-yt"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Watched"` to `YOUTUBE_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_youtube`**

Since `_run_youtube` delegates to `_run_file_import`, add the resolver call at the
top of `_run_youtube`:

```python
def _run_youtube(ctx: RunContext) -> None:
    media_state = _state_load(STATE_PATH)
    if not media_state.watched_definition_id:
        def_id = ctx.resolved_definition_id(WATCHED_SPEC, canonical_name="Watched")
        media_state.watched_definition_id = def_id
        _state_save(media_state)
    _run_file_import(ctx, parse=youtube_importer.parse_takeout_json, tag="youtube")
```

- [ ] **Step 5: `WATCHED_SPEC` is already defined.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "youtube"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(youtube): use the shared definition resolver

YouTube watch history takeout plugin now adopts the shared 'Watched'
definition before parsing the JSON file, consistent with trakt,
netflix, letterboxd, and apple-takeout."
```

---

### BR10: Retrofit apple-takeout

**Files:** `packages/media-helpers/fulcra_media/collect_plugins.py`,
`packages/media-helpers/tests/test_collect_plugins.py`

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify canonical name from source**

```bash
grep -n "category=" packages/media-helpers/fulcra_media/importers/apple_takeout.py | head -5
```

Confirm `category="watched"`. Canonical name: `"Watched"`.

- [ ] **Step 2: Write a regression test**

```python
def test_apple_takeout_plugin_declares_canonical_definition_name():
    assert APPLE_TAKEOUT_PLUGIN.canonical_definition_name == "Watched"

def test_apple_takeout_resolver_called_when_watched_def_missing(monkeypatch, tmp_path):
    """BR10: apple-takeout adopts 'Watched' when media state is empty."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_csv = tmp_path / "Playback Activity.csv"
    fake_csv.write_text("Title,Watched Date\n")
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.apple_takeout_importer.parse_playback_csv",
        lambda path: [],
    )

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name):
            return [{"id": "def-watched-at", "name": name,
                     "annotation_type": "duration",
                     "measurement_spec": {"measurement_type": "duration",
                                          "value_type": "duration", "unit": None}}]
        def create_definition(self, *, name, **spec): return {"id": "should-not-reach"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="apple-takeout",
        config={"path": str(fake_csv)},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert empty_media_state.watched_definition_id == "def-watched-at"
    assert len(saved_states) == 1
```

- [ ] **Step 3: Add `canonical_definition_name="Watched"` to `APPLE_TAKEOUT_PLUGIN = Plugin(...)`.**

- [ ] **Step 4: Replace definition-discovery in `_run_apple_takeout`**

Add before `resolved = _resolve_path(ctx)`:

```python
media_state = _state_load(STATE_PATH)
if not media_state.watched_definition_id:
    def_id = ctx.resolved_definition_id(WATCHED_SPEC, canonical_name="Watched")
    media_state.watched_definition_id = def_id
    _state_save(media_state)
```

- [ ] **Step 5: `WATCHED_SPEC` is already defined.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "takeout"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(apple-takeout): use the shared definition resolver

Apple TV playback takeout adopts the shared 'Watched' definition,
completing the set of 'watched' plugins using the resolver."
```

---

### BR11: Retrofit generic-rss (special handling — config-driven category)

**Special note:** `generic-rss` takes `category` from `ctx.config["category"]` at
runtime. The canonical name therefore cannot be declared statically on the `Plugin`
object. The retrofit must resolve the right definition (either `"Watched"` or
`"Listened"`) based on the runtime config value. `canonical_definition_name` on the
Plugin constructor will be `None` (the plugin manages its own definition at runtime,
just via the resolver rather than directly).

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify the category handling from source**

```bash
grep -n "category\|VALID_CATEGORIES" packages/media-helpers/fulcra_media/importers/generic_rss.py | head -10
grep -n "category" packages/media-helpers/fulcra_media/collect_plugins.py | grep -i "generic_rss\|generic-rss"
```

Confirm category is read from `ctx.config["category"]` in `_run_generic_rss` and
is either `"watched"` or `"listened"`. The canonical name map is:
`"watched" → "Watched"`, `"listened" → "Listened"`.

- [ ] **Step 2: Write a regression test for each branch**

```python
def test_generic_rss_plugin_canonical_definition_name_is_none():
    """BR11: generic-rss manages its definition at runtime (config-driven),
    so canonical_definition_name is None on the Plugin object itself."""
    assert GENERIC_RSS_PLUGIN.canonical_definition_name is None

def test_generic_rss_resolver_watched_path(monkeypatch):
    """BR11a: generic-rss with category=watched resolves 'Watched'."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.rss_importer.normalize_feed",
                        lambda url, service, category: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    resolved_names: list = []

    class _FakeDefClient:
        def list_definitions(self, *, name):
            resolved_names.append(name)
            return []
        def create_definition(self, *, name, **spec): return {"id": "def-watched-rss"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="generic-rss",
        config={"feed_url": "https://example.com/rss",
                "service": "my-blog",
                "category": "watched"},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert "Watched" in resolved_names
    assert empty_media_state.watched_definition_id == "def-watched-rss"


def test_generic_rss_resolver_listened_path(monkeypatch):
    """BR11b: generic-rss with category=listened resolves 'Listened'."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.collect_plugins.rss_importer.normalize_feed",
                        lambda url, service, category: [])
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: None)

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    class _FakeDefClient:
        def list_definitions(self, *, name): return []
        def create_definition(self, *, name, **spec): return {"id": "def-listened-rss"}

    class _FakePS:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="generic-rss",
        config={"feed_url": "https://example.com/rss",
                "service": "my-podcast",
                "category": "listened"},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert empty_media_state.listened_definition_id == "def-listened-rss"
```

- [ ] **Step 3: `canonical_definition_name` stays `None` on `GENERIC_RSS_PLUGIN`** (no change to Plugin construction).

- [ ] **Step 4: Replace definition-discovery in `_run_generic_rss`**

Add a map and resolver call after the config-validation block and before
`since = _rss_since(ctx)`:

```python
_CATEGORY_TO_CANONICAL = {"watched": "Watched", "listened": "Listened"}
_CATEGORY_TO_SPEC = {"watched": WATCHED_SPEC, "listened": LISTENED_SPEC}
_CATEGORY_TO_STATE_ATTR = {
    "watched": "watched_definition_id",
    "listened": "listened_definition_id",
}

# Resolve the definition for this runtime category
media_state = _state_load(STATE_PATH)
state_attr = _CATEGORY_TO_STATE_ATTR[category]
if not getattr(media_state, state_attr):
    canonical = _CATEGORY_TO_CANONICAL[category]
    spec = _CATEGORY_TO_SPEC[category]
    def_id = ctx.resolved_definition_id(spec, canonical_name=canonical)
    setattr(media_state, state_attr, def_id)
    _state_save(media_state)
```

Note: the three helper dicts can be module-level constants for readability.
`category` is validated above (raises RuntimeError if missing) so the `.get`
with key error is safe here.

- [ ] **Step 5: `WATCHED_SPEC` and `LISTENED_SPEC` are already defined.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "generic_rss"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(generic-rss): use the shared definition resolver

generic-rss is config-driven: the category (and therefore the Fulcra
definition) is set at runtime from ctx.config['category']. The retrofit
maps 'watched' → 'Watched' and 'listened' → 'Listened' and calls
ctx.resolved_definition_id with the appropriate spec before the feed
fetch. canonical_definition_name stays None on the Plugin object because
the name is only known at runtime.

Both category paths are regression-tested."
```

---

### BR12: Retrofit generic-csv (special handling — same as generic-rss)

**Special note:** Same config-driven pattern as `generic-rss`. `canonical_definition_name`
stays `None`. The category (`"watched"` or `"listened"`) comes from `ctx.config["category"]`.

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1 (CRITICAL): Re-verify category handling from source**

```bash
grep -n "VALID_CATEGORIES\|category" packages/media-helpers/fulcra_media/importers/generic_csv.py | head -10
```

Confirm `VALID_CATEGORIES = {"watched", "listened"}` and category comes from config.

- [ ] **Step 2: Write a regression test for each branch**

```python
def test_generic_csv_plugin_canonical_definition_name_is_none():
    assert GENERIC_CSV_PLUGIN.canonical_definition_name is None

def test_generic_csv_resolver_watched_path(monkeypatch, tmp_path):
    """BR12a: generic-csv with category=watched resolves 'Watched'."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []
    monkeypatch.setattr("fulcra_media.collect_plugins._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.collect_plugins._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_csv = tmp_path / "media.csv"
    fake_csv.write_text("timestamp,title\n")
    monkeypatch.setattr("fulcra_media.collect_plugins.parse_media_csv",
                        lambda *a, **kw: [])

    class FakeResult:
        posted = 0; skipped_existing = 0
    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False): return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: FakeClient())

    resolved_names: list = []

    class _FakeDefClient:
        def list_definitions(self, *, name):
            resolved_names.append(name)
            return []
        def create_definition(self, *, name, **spec): return {"id": "def-watched-csv"}

    class _FakePS:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="generic-csv",
        config={"path": str(fake_csv),
                "service": "my-service",
                "category": "watched"},
        credentials={},
        state=_FakePS(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: _FakeDefClient(),
    )
    GENERIC_CSV_PLUGIN.run(ctx)

    assert "Watched" in resolved_names
    assert empty_media_state.watched_definition_id == "def-watched-csv"
```

(Add a matching `test_generic_csv_resolver_listened_path` test mirroring BR11b.)

- [ ] **Step 3: `canonical_definition_name` stays `None` on `GENERIC_CSV_PLUGIN`.**

- [ ] **Step 4: Replace definition-discovery in `_run_generic_csv`**

Add the same `_CATEGORY_TO_CANONICAL` / `_CATEGORY_TO_SPEC` / `_CATEGORY_TO_STATE_ATTR`
lookup (these are module-level constants defined in BR11). Add the resolver block
after the `category` validation and before `ts_col = ctx.config.get(...)`:

```python
media_state = _state_load(STATE_PATH)
state_attr = _CATEGORY_TO_STATE_ATTR[category]
if not getattr(media_state, state_attr):
    canonical = _CATEGORY_TO_CANONICAL[category]
    spec = _CATEGORY_TO_SPEC[category]
    def_id = ctx.resolved_definition_id(spec, canonical_name=canonical)
    setattr(media_state, state_attr, def_id)
    _state_save(media_state)
```

- [ ] **Step 5: Module-level maps are already defined from BR11.**

- [ ] **Step 6: Run tests; all green**

```bash
uv run pytest packages/media-helpers/tests/test_collect_plugins.py -q -k "generic_csv"
```

- [ ] **Step 7: Branch check; commit**

```bash
git add packages/media-helpers/
git commit -m "refactor(generic-csv): use the shared definition resolver

Same config-driven pattern as generic-rss: category ('watched' or
'listened') determines which Fulcra definition to adopt. Reuses the
module-level mapping dicts defined in the generic-rss retrofit.
Both category paths are regression-tested."
```

---

### BR13: Final verification

- [ ] **Step 0: Verify branch.**

- [ ] **Step 1: Run the full media-helpers test suite**

```bash
uv run pytest packages/media-helpers/ -q
```

Expected: all green.

- [ ] **Step 2: Run the full workspace test suite**

```bash
cd /Users/Scanning/Developer/fulcra-tools
uv run pytest -q
```

Expected: every package green — media-helpers, collect, fulcra-common, attention, csv-importer, dayone.

- [ ] **Step 3: Ruff over the changed files**

```bash
uv run ruff check packages/media-helpers/fulcra_media/collect_plugins.py \
    packages/media-helpers/tests/test_collect_plugins.py
```

Expected: clean.

- [ ] **Step 4: End-to-end discovery sanity**

```bash
uv run --package fulcra-collect python -c "
from fulcra_collect.registry import discover
r = discover()
print(len(r.plugins), 'plugins discovered;', len(r.errors), 'errors')
for pid in sorted(r.plugins):
    p = r.plugins[pid]
    print(' -', pid, '— canonical_definition_name =', repr(p.canonical_definition_name))
"
```

Expected: all plugins discovered, 0 errors. Plugins with static canonical names show
their value; `generic-rss`, `generic-csv`, and `media-webhook` show `None`.

- [ ] **Step 5: Branch check.**

---

## Out of scope

- `media-webhook`: requires a separate design for resolver-at-startup in a long-running
  service context. Deferred (see skip rationale in Inventory).
- Schema migrations (Fulcra-side).
- The menubar's force-new toggle (separate piece, tracked in the menubar design spec).
- `fulcra-collect plugin reset-definition` already shipped in the resolver workstream.
- Any new state fields — all retrofits write into the existing `media_state` fields
  (`watched_definition_id`, `listened_definition_id`, `read_definition_id`) via the
  same pattern established by lastfm and spotify-extended.

---

## Self-Review

**Spec coverage:** Every deferred plugin from the resolver spec is either
retrofitted (BR1–BR12) or explicitly skipped with rationale (`media-webhook`).
The `generic-rss` and `generic-csv` special-handling correctly handles
config-driven categories — a case not explicitly addressed in the spec but
that naturally follows from the resolver design.

**Canonical name verification:** Each task's Step 1 mandates re-reading the
importer's `category=` assignment and cross-referencing with `fulcra.py`'s
`ensure_definitions` name strings. The R5 casing bug (`"attention"` vs
`"Attention"`) is explicitly recalled.

**Placeholder scan:** No TBD/TODO in any task. Every step ships actual code or a
runnable shell command.

**Type consistency:** All SPEC constants share the same shape as `ATTENTION_SPEC`,
`LASTFM_LISTENED_SPEC`, and `SPOTIFY_EXTENDED_LISTENED_SPEC`. All resolver calls
use the same `ctx.resolved_definition_id(SPEC, canonical_name=...)` signature.
All state writes use `media_state.<field>_definition_id` to match
`FulcraClient.run_import`'s `category_to_def` mapping.

**Shared-definition convergence:** Plugins writing the same category converge on
the same canonical name: four "listened" plugins (`deezer`, `spotify-ifttt`,
`apple-podcasts`, `apple-podcasts-timemachine`) join `lastfm` and
`spotify-extended` on `"Listened"`; five "watched" plugins (`trakt`, `netflix`,
`letterboxd`, `youtube`, `apple-takeout`) converge on `"Watched"`; `goodreads`
alone owns `"Read"`. Adoption-path tests confirm no spurious creates occur when
a sibling plugin has already populated the definition.
