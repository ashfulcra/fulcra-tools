"""Tests for the ported annotations writer (`fulcra_common.annotations`).

The writer deliberately uses stdlib ``urllib`` for the record POST (not httpx),
so — unlike the rest of fulcra-common's I/O tests, which inject an httpx
MockTransport — these tests STUB THE URLLIB OPENER (a ``_Router`` patched over
``urllib.request.urlopen``) and stub the CLI shell-outs (``_fulcra_cli_json`` /
``_fulcra_cli_json_lines`` / ``_fulcra_cli_lines_or_error``) and the token
resolver. No test here ever touches the network or a real ``fulcra`` CLI, except
the explicitly-marked integration test at the bottom (skipped in CI).

Coverage:
  * request path / content-type / body-per-line pinned for the typed endpoint,
    for both a base-type record and a custom-definition record (the def is
    referenced via ``sources``, the path is STILL the base type);
  * fail-closed resolution — a catalog LOOKUP ERROR must NOT create a
    definition and the write must refuse (return False, no POST); a
    verified-absent definition creates EXACTLY once; an in-run cache prevents
    re-lookup;
  * deterministic duplicate handling — 3 same-name defs resolve to the OLDEST
    (created_at ascending), create is never called, a warning is emitted.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import fulcra_common.annotations as ann


# ---------------------------------------------------------------------------
# urllib opener stub (the writer POSTs the record over stdlib urllib)
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal context-manager stand-in for http.client.HTTPResponse."""

    def __init__(self, body=b"", status=201):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self._body = body or b""
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Router:
    """Records every urlopen call; answers 201 {"upload_id": ...} by default.

    Each recorded call is ``(method, full_url, body_bytes, headers)`` so a test
    can pin the path, content-type, and per-line body of the record POST."""

    def __init__(self, response=None):
        self.calls: list[tuple[str, str, bytes, dict]] = []
        self._response = response or _FakeResp({"upload_id": "up-test-1"}, 201)

    def __call__(self, req, *args, **kwargs):
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((req.get_method(), req.full_url, req.data, headers))
        resp = self._response
        if isinstance(resp, Exception):
            raise resp
        return resp

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test isolation: tmp cache/config, a fake token, a stub API base,
    and a cleared in-run definition memo so nothing leaks across tests."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("FULCRA_API_BASE", "https://api.example.test")
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "tkn-test")
    monkeypatch.setenv("FULCRA_COORD_ANNOTATIONS", "on")
    monkeypatch.setattr(ann, "_resolve_token", lambda: "tkn-test")
    ann._DEF_ID_MEMO.clear()
    yield
    ann._DEF_ID_MEMO.clear()


def _install_router(monkeypatch, response=None) -> _Router:
    router = _Router(response=response)
    monkeypatch.setattr(ann.urllib.request, "urlopen", router)
    return router


def _stub_cli(monkeypatch, *, catalog_lines, create_id="def-created"):
    """Stub the three CLI shell-outs.

    ``catalog_lines``: what ``_fulcra_cli_lines_or_error(["catalog", ...])``
    returns (a list, or None to simulate a CLI/lookup error). ``tag get``
    yields a deterministic ``tag-<name>``; ``data-type create`` yields
    ``create_id``. Returns a ``calls`` list recording ``_fulcra_cli_json``
    invocations so a test can assert create was / was not called."""
    calls: list[list] = []

    def fake_json(args, **k):
        calls.append(list(args))
        if args[:2] == ["tag", "get"]:
            return {"id": f"tag-{args[2]}"}
        if args[:2] == ["data-type", "create"]:
            return {"id": create_id}
        return None

    monkeypatch.setattr(ann, "_fulcra_cli_json", fake_json)
    monkeypatch.setattr(ann, "_fulcra_cli_lines_or_error",
                        lambda args, **k: catalog_lines)
    # Legacy sibling kept in sync for any incidental callers.
    monkeypatch.setattr(ann, "_fulcra_cli_json_lines",
                        lambda args, **k: (catalog_lines or []))
    return calls


def _payload(lifecycle="complete", agent="claude-code:mb:repo"):
    task = {
        "id": "20260708-fix-widget",
        "title": "Fix the widget pipeline",
        "workstream": "devops",
        "tags": ["kind:bug"],
        "current_summary": "rewiring",
        "next_action": "ship it",
        "events": [{"at": "2026-07-08T12:00:00.000000Z", "type": "complete"}],
    }
    return ann.build_annotation(lifecycle=lifecycle, task=task, agent=agent)


def _existing_def_row(uuid="def-existing", name=None, created_at="2026-01-01T00:00:00Z"):
    return {
        "id": f"MomentAnnotation/{uuid}",
        "name": name or ann.DEFINITION_NAME,
        "column_name": "moment",
        "deprecated": False,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Typed endpoint: path / content-type / body-per-line
# ---------------------------------------------------------------------------

def test_record_posts_to_base_typed_endpoint_not_batch(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    router = _install_router(monkeypatch)

    assert ann._write_http(_payload()) is True

    posts = router.posts()
    assert len(posts) == 1, "exactly one record POST"
    _, url, body, headers = posts[0]
    # Typed endpoint, base type as the path segment.
    assert url == "https://api.example.test/ingest/v1/record/MomentAnnotation"
    # NOT the deprecated legacy batch endpoint.
    assert "/ingest/v1/record/batch" not in url
    # NOT the custom-uuid path segment (that 404s per the live verification).
    assert "/MomentAnnotation/def-existing" not in url
    assert headers.get("content-type") == "application/x-jsonl"
    assert "content-length" in headers


def test_body_is_one_flat_record_per_line(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    router = _install_router(monkeypatch)

    ann._write_http(_payload())

    body = router.posts()[0][2].decode()
    lines = [ln for ln in body.split("\n") if ln.strip()]
    assert len(lines) == 1, "one record per line (single-record jsonlines)"
    rec = json.loads(lines[0])
    # Flat, UNWRAPPED typed body — NOT the DataRecordV1 envelope.
    assert "data" not in rec and "metadata" not in rec and "specversion" not in rec
    assert "note" in rec
    assert "recorded_at" in rec
    assert isinstance(rec["tags"], list)
    # sources — PLURAL — for the typed endpoint (legacy used metadata.source).
    assert isinstance(rec["sources"], list)
    # The typed schema is flat and CLOSED — {note, recorded_at, tags, sources,
    # id}. Any extra top-level key (notably `title`) is SILENTLY STRIPPED by the
    # server, so the writer must not emit one. Guard: keys ⊆ the served set.
    assert "title" not in rec
    assert set(rec) <= {"note", "recorded_at", "tags", "sources", "id"}


def test_title_folded_into_note_no_title_key(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    router = _install_router(monkeypatch)

    ann._write_http(_payload())

    rec = json.loads(router.posts()[0][2].decode().strip())
    # `title` is a NON-served key the typed endpoint silently strips — it must
    # NOT be emitted. The title line (which carries the task_id) is folded into
    # `note`, the one served free-text slot, so nothing is lost.
    assert "title" not in rec
    # Title text (incl. the task_id) now lands inside `note`...
    assert "complete: Fix the widget pipeline" in rec["note"]
    assert "20260708-fix-widget" in rec["note"]
    # ...alongside the original desc/summary body.
    assert "rewiring" in rec["note"]


def test_digest_folds_title_into_note_no_title_key(monkeypatch):
    # The OTHER build site — emit_digest_annotation — must also never emit a
    # top-level `title`; the digest name is folded into `note`.
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    router = _install_router(monkeypatch)

    assert ann.emit_digest_annotation(
        name="Morning digest — 3 blocked on you",
        note="tasks: ship-widget, review-pr",
        window="morning",
        agent="claude-code:mb:repo",
    ) is True

    rec = json.loads(router.posts()[0][2].decode().strip())
    assert "title" not in rec
    assert set(rec) <= {"note", "recorded_at", "tags", "sources", "id"}
    # Both the digest title line and its body survive inside `note`.
    assert "Morning digest — 3 blocked on you" in rec["note"]
    assert "ship-widget" in rec["note"]


def test_custom_definition_referenced_in_sources_on_base_path(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row(uuid="def-existing")])
    router = _install_router(monkeypatch)

    ann._write_http(_payload())

    _, url, body, _ = router.posts()[0]
    assert url.endswith("/ingest/v1/record/MomentAnnotation")  # base path, always
    rec = json.loads(body.decode().strip())
    assert "com.fulcradynamics.annotation.def-existing" in rec["sources"]
    # The per-transition fulcra-coord source id also rides along.
    assert any(s.startswith("com.fulcradynamics.fulcra-coord.complete.")
               for s in rec["sources"])


def test_resolved_tag_ids_land_in_tags(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    router = _install_router(monkeypatch)

    ann._write_http(_payload())

    rec = json.loads(router.posts()[0][2].decode().strip())
    assert all(t.startswith("tag-") for t in rec["tags"])
    assert "tag-agent-tasks" in rec["tags"]


# ---------------------------------------------------------------------------
# Fail-closed resolution (the 2026-07-03 definition-proliferation root cause)
# ---------------------------------------------------------------------------

def test_lookup_error_refuses_to_create(monkeypatch):
    # catalog lookup errors (CLI rc!=0 / timeout) -> None sentinel.
    calls = _stub_cli(monkeypatch, catalog_lines=None)

    got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "desc", ["agent-tasks"])

    assert got == "", "lookup error must resolve to empty (refuse), never a new id"
    assert not any(c[:2] == ["data-type", "create"] for c in calls), \
        "must NOT create a definition when the lookup itself failed"


def test_lookup_error_makes_write_refuse_no_post(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=None)
    router = _install_router(monkeypatch)

    assert ann._write_http(_payload()) is False
    assert router.posts() == [], "no record may be written when resolution fails closed"


def test_verified_absent_creates_exactly_once(monkeypatch):
    calls = _stub_cli(monkeypatch, catalog_lines=[], create_id="def-new")

    got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "desc", ["agent-tasks"])

    assert got == "def-new"
    creates = [c for c in calls if c[:2] == ["data-type", "create"]]
    assert len(creates) == 1, "verified-absent creates exactly once"


# ---------------------------------------------------------------------------
# RAW-output fail-closed: these feed real subprocess stdout THROUGH the parse
# layer (via a `printf` CLI base) instead of stubbing the pre-parsed list. The
# earlier stubs sat ABOVE the parser, so catalog-shape drift and non-JSON
# banners — the actual 2026-07-03 fail-OPEN mechanisms — went unexercised.
# ---------------------------------------------------------------------------

def _printf_backend(monkeypatch, raw_stdout: str):
    """Make the resolved CLI base a ``printf`` that emits ``raw_stdout`` verbatim
    at rc==0 (the appended ``catalog --name ...`` args are ignored by printf,
    which has no conversion specs). This routes ``_resolve_def_via_cli`` through
    the REAL ``_fulcra_cli_lines_or_error`` parser."""
    monkeypatch.setattr(ann, "_cli_base_cmd", lambda: ["printf", raw_stdout])


def test_rc0_unrecognized_same_name_shape_refuses_no_create(monkeypatch):
    # rc==0 catalog carrying a same-name entry in a THIRD, unrecognized shape
    # (neither legacy metadata.moment nor current MomentAnnotation/ top-id). An
    # unreadable same-name entry is NOT "verified absent" — creating would be the
    # fail-OPEN. Fed as RAW stdout so the classifier sees it for real.
    raw = '{"name": "Agent Tasks", "kind": "brand_new_shape", "ref": "xyz"}\n'
    _printf_backend(monkeypatch, raw)
    creates: list[list] = []
    monkeypatch.setattr(
        ann, "_fulcra_cli_json",
        lambda args, **k: creates.append(list(args)) or {"id": "SHOULD-NOT"})

    got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "desc", ["agent-tasks"])

    assert got == "", "an unreadable same-name entry is NOT verified-absent; must refuse"
    assert creates == [], "must NOT create when a same-name entry is in an unknown shape"


def test_rc0_non_json_banner_is_lookup_error_refuses_no_create(monkeypatch):
    # rc==0 stdout that is a plain-text banner (format drift / warning), not JSON.
    # The parser must read "non-empty lines, zero parsed" as a lookup ERROR (None),
    # never as an empty catalog ([]), so resolution refuses rather than creates.
    raw = "WARNING: fulcra CLI catalog output format changed; re-auth required\n"
    _printf_backend(monkeypatch, raw)
    creates: list[list] = []
    monkeypatch.setattr(
        ann, "_fulcra_cli_json",
        lambda args, **k: creates.append(list(args)) or {"id": "SHOULD-NOT"})

    got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "desc", ["agent-tasks"])

    assert got == "", "a non-JSON banner is a lookup error, not an empty catalog; must refuse"
    assert creates == [], "must NOT create when the catalog reply was unparseable"


def test_in_run_cache_prevents_relookup(monkeypatch):
    lookups = []

    def fake_lines(args, **k):
        lookups.append(list(args))
        return [_existing_def_row(uuid="def-x")]

    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row(uuid="def-x")])
    monkeypatch.setattr(ann, "_fulcra_cli_lines_or_error", fake_lines)

    a = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "d", [])
    b = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "d", [])

    assert a == b == "def-x"
    assert len(lookups) == 1, "second resolve must hit the in-run memo, not the CLI"


# ---------------------------------------------------------------------------
# Deterministic duplicate handling (existing dup defs already in the catalog)
# ---------------------------------------------------------------------------

def test_three_duplicates_pick_oldest_no_create(monkeypatch, caplog):
    rows = [
        _existing_def_row(uuid="ddd-newest", created_at="2026-07-03T09:00:00Z"),
        _existing_def_row(uuid="aaa-oldest", created_at="2026-01-01T00:00:00Z"),
        _existing_def_row(uuid="mmm-middle", created_at="2026-04-15T00:00:00Z"),
    ]
    calls = _stub_cli(monkeypatch, catalog_lines=rows)

    with caplog.at_level("WARNING"):
        got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "d", [])

    assert got == "aaa-oldest", "oldest created_at ascending is chosen"
    assert not any(c[:2] == ["data-type", "create"] for c in calls), \
        "must NEVER create another when duplicates already exist"
    # A warning naming all candidate ids is emitted.
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "aaa-oldest" in warned and "ddd-newest" in warned and "mmm-middle" in warned


def test_duplicate_uuid_tiebreak_when_no_created_at(monkeypatch):
    # Rows lacking created_at fall back to deterministic uuid ordering so every
    # host converges on the same pick.
    rows = [
        {"id": "MomentAnnotation/bbb", "name": ann.DEFINITION_NAME,
         "column_name": "moment", "deprecated": False},
        {"id": "MomentAnnotation/aaa", "name": ann.DEFINITION_NAME,
         "column_name": "moment", "deprecated": False},
    ]
    _stub_cli(monkeypatch, catalog_lines=rows)
    got = ann._resolve_def_via_cli(ann.DEFINITION_NAME, "d", [])
    assert got == "aaa"


# ---------------------------------------------------------------------------
# Best-effort contract: a urllib error never propagates
# ---------------------------------------------------------------------------

def test_write_is_best_effort_on_http_error(monkeypatch):
    _stub_cli(monkeypatch, catalog_lines=[_existing_def_row()])
    err = urllib.error.HTTPError("http://x", 500, "err", None, io.BytesIO(b""))
    _install_router(monkeypatch, response=err)

    # Must swallow and report False, never raise into the caller's task op.
    assert ann._write_http(_payload()) is False


# ---------------------------------------------------------------------------
# Timeout-knob parity: the port must honor the LEGACY FULCRA_COORD_TIMEOUT_SECONDS
# read knob with its max(60, ...) write floor (not a bespoke new-only var).
# ---------------------------------------------------------------------------

def test_write_timeout_honors_legacy_knob(monkeypatch):
    monkeypatch.delenv("FULCRA_COORD_WRITE_TIMEOUT", raising=False)
    monkeypatch.delenv("FULCRA_COORD_TIMEOUT_SECONDS", raising=False)
    # unset -> legacy read default (30), floored up to 60
    assert ann._write_timeout() == 60
    # a legacy value above the floor applies verbatim
    monkeypatch.setenv("FULCRA_COORD_TIMEOUT_SECONDS", "120")
    assert ann._write_timeout() == 120
    # a legacy value below the floor is clamped up (max(60, ...))
    monkeypatch.setenv("FULCRA_COORD_TIMEOUT_SECONDS", "10")
    assert ann._write_timeout() == 60
    # a non-numeric legacy value falls back to the default, still floored
    monkeypatch.setenv("FULCRA_COORD_TIMEOUT_SECONDS", "not-a-number")
    assert ann._write_timeout() == 60
    # the explicit write override wins, with its own floor of 1
    monkeypatch.setenv("FULCRA_COORD_WRITE_TIMEOUT", "5")
    assert ann._write_timeout() == 5


# ---------------------------------------------------------------------------
# Import shadowing: `from fulcra_common import annotations` must yield the
# submodule, not the __future__._Feature bound by the package __init__.
# ---------------------------------------------------------------------------

def test_annotations_submodule_not_shadowed_by_future_import():
    # A genuine guard must run in a FRESH interpreter: this test module already
    # does `import fulcra_common.annotations` at the top, which registers the
    # submodule as a package attribute and masks the shadow. Spawn a subprocess
    # whose ONLY access is the from-form.
    import subprocess
    import sys
    code = (
        "from fulcra_common import annotations as m; import sys; "
        "sys.exit(0 if hasattr(m, 'emit_lifecycle_annotation') else 1)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        "`from fulcra_common import annotations` yielded __future__._Feature, not "
        "the submodule: " + (r.stderr or r.stdout))


# ---------------------------------------------------------------------------
# Marked integration test — one real write + read. Skipped in CI; runnable
# locally with a live Fulcra auth (`FULCRA_INTEGRATION=1`). ONE record max,
# clearly-marked test content.
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="integration: requires live Fulcra auth; run locally "
                         "with FULCRA_INTEGRATION=1")
def test_integration_real_write_and_read():
    import os
    import subprocess
    from datetime import datetime, timezone

    if not os.environ.get("FULCRA_INTEGRATION"):
        pytest.skip("set FULCRA_INTEGRATION=1 to run the live round-trip")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    task = {
        "id": "integration-test",
        "title": f"annotations-port integration test {stamp} (safe to delete)",
        "workstream": "test",
        "tags": ["kind:other"],
        "current_summary": "typed-endpoint round-trip verification",
        "events": [{"at": stamp.replace("+00:00", "Z"), "type": "complete"}],
    }
    payload = ann.build_annotation(
        lifecycle="complete", task=task, agent="claude-code:ci:annotations-port")
    assert ann._write_http(payload) is True

    def_id = ann._resolve_definition_id(payload.get("cli_tags") or [])
    assert def_id
    out = subprocess.run(
        [*ann._cli_base_cmd(), "get-records", f"MomentAnnotation/{def_id}",
         f"{stamp[:10]} to now"],
        capture_output=True, text=True, timeout=60,
    )
    assert "integration test" in out.stdout
