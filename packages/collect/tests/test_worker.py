"""The worker entrypoint — runs one plugin, emits JSON-line events."""
from __future__ import annotations

import io
import json
from pathlib import Path

from fulcra_collect import worker
from fulcra_collect.plugin import Plugin, RunContext
from fulcra_collect.registry import RegistryResult
from fulcra_collect.worker import _scrub_secrets


def _run_capturing(plugin: Plugin, collect_home: Path) -> list[dict]:
    """Run a plugin through the worker, return the emitted JSON events."""
    buf = io.StringIO()
    worker.run_plugin(plugin, out=buf)
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_worker_emits_a_done_result_for_a_successful_run(collect_home: Path):
    plugin = Plugin(id="ok", name="OK", kind="manual", collect_mode="historical", run=lambda ctx: None)
    events = _run_capturing(plugin, collect_home)
    assert events[-1] == {"type": "result", "outcome": "done",
                          "error": None, "watermark": None,
                          "definition_id": None,
                          "definition_validated_at": None}


def test_worker_carries_the_watermark_set_by_the_plugin(collect_home: Path):
    def run(ctx):
        ctx.state.watermark = "2026-05-22T12:00:00Z"
    plugin = Plugin(id="wm", name="WM", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["watermark"] == "2026-05-22T12:00:00Z"


def test_worker_carries_the_definition_id_set_by_the_plugin(collect_home: Path):
    """Important 1: definition_id written to ctx.state during the run must
    appear in the result event so the runner can persist it.  Mirrors the
    watermark round-trip test above."""
    def run(ctx):
        ctx.state.definition_id = "def-xyz789"
    plugin = Plugin(id="defid", name="DefId", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["definition_id"] == "def-xyz789"


def test_worker_carries_definition_id_on_error_path(collect_home: Path):
    """Important 1: the resolver may succeed before the plugin crashes;
    definition_id must still appear in the error-path result event so we
    don't re-resolve on the next run."""
    def run(ctx):
        ctx.state.definition_id = "def-partial"
        raise RuntimeError("plugin crashed after resolving")
    plugin = Plugin(id="defid-err", name="DefId-Err", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["outcome"] == "error"
    assert events[-1]["definition_id"] == "def-partial"


def test_worker_wires_persistent_plugin_kv_with_plugin_isolation(
    collect_home: Path,
):
    seen: list[object] = []

    def first_run(ctx: RunContext) -> None:
        assert ctx.kv_get("counter", 0) == 0
        assert ctx.kv_update(
            "counter", lambda current: current + 1, default=0,
        ) == 1
        ctx.kv_set("checkpoint", {"page": 4})

    first = Plugin(
        id="service-a", name="Service A", kind="service",
        collect_mode="live_continuous", run=first_run,
    )
    assert _run_capturing(first, collect_home)[-1]["outcome"] == "done"

    def restarted(ctx: RunContext) -> None:
        seen.append(ctx.kv_get("counter"))
        seen.append(ctx.kv_get("checkpoint"))
        seen.append(ctx.kv_delete("checkpoint"))

    second = Plugin(
        id="service-a", name="Service A", kind="service",
        collect_mode="live_continuous", run=restarted,
    )
    assert _run_capturing(second, collect_home)[-1]["outcome"] == "done"
    assert seen == [1, {"page": 4}, True]

    isolated: list[object] = []
    other = Plugin(
        id="service-b", name="Service B", kind="service",
        collect_mode="live_continuous",
        run=lambda ctx: isolated.append(ctx.kv_get("counter", "missing")),
    )
    assert _run_capturing(other, collect_home)[-1]["outcome"] == "done"
    assert isolated == ["missing"]


def test_worker_forwards_progress_events(collect_home: Path):
    def run(ctx):
        ctx.progress(done=1, total=3)
        ctx.progress(done=3, total=3)
    plugin = Plugin(id="p", name="P", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    progress = [e for e in events if e["type"] == "progress"]
    assert progress == [
        {"type": "progress", "done": 1, "total": 3},
        {"type": "progress", "done": 3, "total": 3},
    ]
    assert events[-1]["outcome"] == "done"


def test_worker_emits_an_error_result_when_run_raises(collect_home: Path):
    def run(ctx):
        raise RuntimeError("kaboom")
    plugin = Plugin(id="bad", name="Bad", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["type"] == "result"
    assert events[-1]["outcome"] == "error"
    assert "kaboom" in events[-1]["error"]


def test_main_reports_unknown_plugin_id(collect_home: Path, capsys):
    rc = worker.main(["no-such-plugin"], registry=RegistryResult())
    captured = capsys.readouterr()
    last = [ln for ln in captured.out.splitlines() if ln][-1]
    import json as _json
    assert _json.loads(last)["outcome"] == "error"
    assert rc == 1


def test_scrub_secrets_redacts_a_url_query_param():
    """M1: a secret-named URL query value is replaced, non-secret params kept."""
    text = "GET https://api.x/v1?api_key=ABC123&page=2 failed"
    scrubbed = _scrub_secrets(text)
    assert "ABC123" not in scrubbed
    assert "api_key=<redacted>" in scrubbed
    assert "page=2" in scrubbed  # non-secret param untouched


def test_scrub_secrets_redacts_a_bearer_token():
    """M1: a Bearer token in a traceback message is replaced."""
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SECRETPART"
    scrubbed = _scrub_secrets(text)
    assert "SECRETPART" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_secrets_leaves_non_secret_text_intact():
    """M1: ordinary error text passes through unchanged."""
    text = "RuntimeError: connection reset by peer at line 42"
    assert _scrub_secrets(text) == text


def test_scrub_secrets_truncates_a_pathological_traceback():
    """M1: the result is bounded so a huge traceback can't bloat state."""
    scrubbed = _scrub_secrets("x" * 10_000)
    assert len(scrubbed) <= 4000 + len("… (truncated)")
    assert scrubbed.endswith("… (truncated)")


def test_worker_error_result_scrubs_a_secret_in_the_exception(collect_home: Path):
    """M1: a secret raised in a plugin exception never reaches the event."""
    def run(ctx):
        raise RuntimeError("auth failed for https://api.x/v1?token=TOPSECRET")
    plugin = Plugin(id="leaky", name="Leaky", kind="manual", collect_mode="historical", run=run)
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["outcome"] == "error"
    assert "TOPSECRET" not in events[-1]["error"]
    assert "token=<redacted>" in events[-1]["error"]


def test_worker_isolates_plugin_stdout_from_event_stream(
    collect_home: Path, monkeypatch, capsys,
):
    """Finding 9: a stray print() inside plugin.run must NOT corrupt the JSON
    event stream. The worker's runner parses `out` via splitlines() + json.loads
    and silently skips non-JSON lines, so a plain print() that lands between
    the progress and result emits would cause the result to be silently lost —
    the run is recorded as 'error' (no result emitted) and any watermark the
    plugin advanced gets dropped.

    Fix contract: stdout writes from *inside* plugin.run get redirected to
    stderr for the duration of the run, while the JSON event stream still
    goes to the `out` parameter the worker captured before the call. Mirrors
    the real worker entrypoint in `main()`, which passes the real `sys.stdout`
    as `out` — so `out` and `sys.stdout` are the same stream at call time.
    """
    import sys

    def run(ctx):
        # A library somewhere calls print(); the worker must not let this leak
        # into the JSON event stream that `out` carries.
        print("hello from a noisy library")
        ctx.state.watermark = "2026-05-22T12:00:00Z"

    plugin = Plugin(id="noisy", name="Noisy", kind="manual", collect_mode="historical", run=run)
    # Re-bind sys.stdout to a buffer and pass it as `out` — same identity, as
    # in worker.main(). The whole point is that a stray print() (which writes
    # to whatever sys.stdout currently is) must not land on `out`.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    worker.run_plugin(plugin, out=buf)
    # Restore stdout for capsys before assertions.
    monkeypatch.undo()

    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    # Every line on `out` must be valid JSON — no stray "hello..." string.
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[-1]["type"] == "result"
    assert parsed[-1]["outcome"] == "done"
    assert parsed[-1]["watermark"] == "2026-05-22T12:00:00Z"


def test_worker_fails_fast_when_a_required_credential_is_missing(collect_home: Path):
    from fulcra_collect.plugin import Credential
    ran = []
    plugin = Plugin(id="needs-key", name="Needs Key", kind="manual",
                    collect_mode="historical",
                    run=lambda ctx: ran.append(True),
                    required_credentials=(Credential(key="api-key", label="K", help="h"),))
    events = _run_capturing(plugin, collect_home)
    assert ran == []  # run() was never called
    assert events[-1]["type"] == "result"
    assert events[-1]["outcome"] == "error"
    assert "api-key" in events[-1]["error"]


def test_worker_reads_user_level_credentials_from_user_store(
        collect_home: Path, monkeypatch):
    """P3 #14: a Credential declared ``user_level=True`` lives in the
    account-scoped keychain store (``get_user_secret``), not the
    plugin-scoped one — the daemon's set/status/delete paths already
    route by that flag. The worker built ctx.credentials from the
    plugin-scoped store only, so the first plugin declaring a required
    user-level credential would falsely fail "missing required
    credential" every run."""
    from fulcra_collect.plugin import Credential

    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: "user-scoped-tok" if key == "bearer-token" else None,
    )
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_secret",
        lambda plugin_id, key: "plugin-scoped-val" if key == "api-key" else None,
    )

    seen: list[dict] = []
    plugin = Plugin(
        id="mixed-creds", name="Mixed Creds", kind="manual",
        collect_mode="historical",
        run=lambda ctx: seen.append(dict(ctx.credentials)),
        required_credentials=(
            Credential(key="bearer-token", label="T", help="h",
                       user_level=True),
            Credential(key="api-key", label="K", help="h"),
        ),
    )
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["outcome"] == "done", events[-1]
    assert seen == [{"bearer-token": "user-scoped-tok",
                     "api-key": "plugin-scoped-val"}]


def test_worker_missing_user_level_credential_still_fails_fast(
        collect_home: Path, monkeypatch):
    """Companion to the routing test above: an ABSENT user-level
    credential still produces the fail-fast missing-credential error."""
    from fulcra_collect.plugin import Credential

    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret", lambda key: None)
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_secret", lambda plugin_id, key: None)

    ran: list = []
    plugin = Plugin(
        id="needs-user-cred", name="Needs User Cred", kind="manual",
        collect_mode="historical", run=lambda ctx: ran.append(True),
        required_credentials=(
            Credential(key="bearer-token", label="T", help="h",
                       user_level=True),
        ),
    )
    events = _run_capturing(plugin, collect_home)
    assert ran == []
    assert events[-1]["outcome"] == "error"
    assert "bearer-token" in events[-1]["error"]


# ---------------------------------------------------------------------------
# R5: worker supplies _fulcra_client_factory in RunContext
# ---------------------------------------------------------------------------


def test_worker_passes_non_none_factory_to_run_context(collect_home: Path):
    """R5: run_plugin must construct RunContext with a non-None
    _fulcra_client_factory so every plugin that opts into
    canonical_definition_name can call ctx.resolved_definition_id."""
    received: list = []

    def run(ctx: RunContext) -> None:
        received.append(ctx._fulcra_client_factory)

    plugin = Plugin(id="factory-check", name="Factory Check",
                    kind="manual", collect_mode="historical", run=run)
    _run_capturing(plugin, collect_home)
    assert len(received) == 1
    factory = received[0]
    assert factory is not None, "RunContext._fulcra_client_factory must not be None"
    assert callable(factory), "_fulcra_client_factory must be callable"


def test_worker_factory_returns_object_with_resolver_interface(collect_home: Path):
    """R5: the factory returned by the worker must produce an object with
    list_definitions and create_definition methods — the interface
    resolve_definition_id calls. We only check method presence (calling them
    without a real Fulcra would fail); functional correctness is covered by
    the adapter unit test below."""
    received: list = []

    def run(ctx: RunContext) -> None:
        received.append(ctx._fulcra_client_factory)

    plugin = Plugin(id="factory-iface", name="Factory Iface",
                    kind="manual", collect_mode="historical", run=run)
    _run_capturing(plugin, collect_home)
    factory = received[0]
    client = factory()
    assert hasattr(client, "list_definitions"), (
        "factory-produced client must have list_definitions"
    )
    assert hasattr(client, "create_definition"), (
        "factory-produced client must have create_definition"
    )


def test_fulcra_definition_adapter_list_definitions_filters_deleted(
    monkeypatch,
):
    """Unit test for _FulcraDefinitionAdapter.list_definitions: only live
    (non-deleted) definitions with the matching name are returned."""
    import httpx
    from fulcra_collect.worker import _FulcraDefinitionAdapter
    from fulcra_common import BaseFulcraClient

    all_defs = [
        {"id": "d1", "name": "attention", "annotation_type": "duration",
         "deleted_at": None, "created_at": "2026-01-01T00:00:00Z"},
        # same name but soft-deleted — must be excluded
        {"id": "d2", "name": "attention", "annotation_type": "duration",
         "deleted_at": "2026-02-01T00:00:00Z", "created_at": "2025-12-01T00:00:00Z"},
        # different name — must be excluded
        {"id": "d3", "name": "last.fm listens", "annotation_type": "moment",
         "deleted_at": None, "created_at": "2026-01-15T00:00:00Z"},
    ]

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/user/v1alpha1/annotation"
        return httpx.Response(200, json=all_defs)

    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    base = BaseFulcraClient(transport=httpx.MockTransport(handler))
    adapter = _FulcraDefinitionAdapter(base)
    result = adapter.list_definitions(name="attention")
    assert result == [all_defs[0]]  # only the live "attention" definition


def test_fulcra_definition_adapter_create_definition_posts_body(monkeypatch):
    """Unit test for _FulcraDefinitionAdapter.create_definition: the POST body
    contains name + all spec fields, and the response id is returned."""
    import httpx
    from fulcra_collect.worker import _FulcraDefinitionAdapter
    from fulcra_common import BaseFulcraClient

    posted: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/user/v1alpha1/annotation"
        assert r.method == "POST"
        posted.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "new-def-99"})

    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    base = BaseFulcraClient(transport=httpx.MockTransport(handler))
    adapter = _FulcraDefinitionAdapter(base)
    result = adapter.create_definition(
        name="attention",
        annotation_type="duration",
        measurement_spec={"measurement_type": "duration",
                          "value_type": "duration", "unit": None},
    )
    assert result == {"id": "new-def-99"}
    assert posted[0]["name"] == "attention"
    assert posted[0]["annotation_type"] == "duration"
    # Defaults injected because Fulcra rejects bodies missing either field
    # (HTTP 422). Discovered the hard way when task #13's stale-def
    # re-resolution path tried to create a "Listened" def with the bare
    # LASTFM_LISTENED_SPEC and Fulcra returned `loc: [body, duration,
    # description], type: missing`.
    assert posted[0]["tags"] == []
    assert posted[0]["description"] == ""


def test_fulcra_definition_adapter_create_definition_lets_spec_override_defaults(monkeypatch):
    """The defaults must not clobber values the spec actually supplies —
    e.g. attention's duration_definition_payload always includes
    description + tags, those need to survive."""
    import httpx
    from fulcra_collect.worker import _FulcraDefinitionAdapter
    from fulcra_common import BaseFulcraClient

    posted: list[dict] = []
    def handler(r: httpx.Request) -> httpx.Response:
        posted.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "new-def-100"})
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")
    base = BaseFulcraClient(transport=httpx.MockTransport(handler))
    adapter = _FulcraDefinitionAdapter(base)
    adapter.create_definition(
        name="attention",
        description="What the user paid attention to.",
        tags=["tag-a", "tag-b"],
        annotation_type="duration",
    )
    assert posted[0]["description"] == "What the user paid attention to."
    assert posted[0]["tags"] == ["tag-a", "tag-b"]


def test_set_credential_writes_to_declared_scope(
        collect_home: Path, monkeypatch):
    """The write-back must route by the credential's declared scope like the
    read side: rotating a user_level credential must hit the USER store, not
    the plugin store (else the read side keeps serving the stale value)."""
    from fulcra_collect.plugin import Credential

    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret", lambda key: "u-val")
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_secret", lambda pid, key: "p-val")
    writes: list[tuple] = []
    monkeypatch.setattr(
        "fulcra_collect.credentials.set_user_secret",
        lambda k, v: writes.append(("user", k, v)))
    monkeypatch.setattr(
        "fulcra_collect.credentials.set_secret",
        lambda pid, k, v: writes.append(("plugin", pid, k, v)))

    def _rotate(ctx):
        ctx.set_credential("shared-tok", "new-user-val")
        ctx.set_credential("api-key", "new-plugin-val")

    plugin = Plugin(
        id="scopetest", name="Scope Test", kind="manual",
        collect_mode="historical", run=_rotate,
        required_credentials=(
            Credential(key="shared-tok", label="s", help="h",
                       user_level=True),
            Credential(key="api-key", label="a", help="h"),
        ),
    )
    events = _run_capturing(plugin, collect_home)
    assert events[-1]["outcome"] == "done", events[-1]
    assert ("user", "shared-tok", "new-user-val") in writes
    assert ("plugin", "scopetest", "api-key", "new-plugin-val") in writes
