"""The canonical engine uses only Collect's captured transport and authority."""
from types import SimpleNamespace

import pytest

from fulcra_collect.files_io import MissingFile
from fulcra_gmail.collect_relay import CollectCoordTransport
from fulcra_gmail.relay import CoordEngineRelayEmitter, build_directive
from fulcra_gmail.rules import parse_rules


class MemoryFiles:
    def __init__(self):
        self.docs = {}
        self.paths = []

    def read_text(self, path, **kw):
        self.paths.append(path)
        if path not in self.docs:
            raise MissingFile(path)
        return self.docs[path]

    def write_text(self, path, content, **kw):
        self.paths.append(path)
        self.docs[path] = content

    def list_dir(self, path, **kw):
        self.paths.append(path)
        prefix = path.rstrip("/") + "/"
        return [{"name": p[len(prefix):], "is_dir": False, "size": len(c), "mtime": "2026-10-23T12:00:00Z"}
                for p, c in self.docs.items() if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def delete(self, path, **kw):
        self.paths.append(path)
        self.docs.pop(path, None)


def directive():
    rule = parse_rules([{"id": "synthetic", "version": 1, "name": "Synthetic", "match": "x",
                         "actions": ["relay"], "relay_to": "synthetic-recipient"}])[0]
    return build_directive("relay-synthetic-only", rule)


def test_real_engine_reemit_converges_using_only_collect_transport(monkeypatch):
    from coord_engine import cli
    monkeypatch.setattr(cli, "FulcraFileTransport", lambda: pytest.fail("default CLI transport reached"))
    files = MemoryFiles()
    session = SimpleNamespace(guard=lambda **kw: None)
    transport = CollectCoordTransport(session, "synthetic", files=files)
    emitter = CoordEngineRelayEmitter("synthetic", run=transport.run, readback=transport.verify_directive)
    first = emitter.emit(directive())
    assert first.ok
    assert emitter.exists(directive())
    second = emitter.emit(directive())
    assert second.ok and second.slug == first.slug
    assert len([p for p in files.docs if p.startswith("/team/synthetic/task/")]) == 1
    assert all(path.startswith("/team/synthetic/") for path in files.paths)


def test_caught_engine_cancellation_stays_latched_and_no_more_requests(monkeypatch):
    from coord_engine import cli
    allowed = [True]
    calls = []
    def guard(**kw):
        calls.append(kw)
        if not allowed[0]:
            raise RuntimeError("Collect configuration changed")
    transport = CollectCoordTransport(SimpleNamespace(guard=guard), "synthetic", files=MemoryFiles())
    def swallowed(argv, transport):
        allowed[0] = False
        assert transport.read("team/synthetic/_coord/optional.json") is None
        allowed[0] = True  # even a quick sign-back-in cannot revive the captured lifecycle
        assert transport.read("team/synthetic/_coord/optional.json") is None
        return 0
    monkeypatch.setattr(cli, "main", swallowed)
    rc, out = transport.run(["tell", "synthetic", "recipient", "Synthetic"])
    assert rc != 0 and not out
    assert transport.cancelled


@pytest.mark.parametrize("path", ["team/other/task/a.md", "team/synthetic/../other/a.md", "//elsewhere/a", "team/synthetic"])
def test_transport_rejects_paths_outside_the_captured_team(path):
    files = MemoryFiles()
    transport = CollectCoordTransport(SimpleNamespace(guard=lambda **kw: None), "synthetic", files=files)
    assert not transport.write(path, "synthetic")
    assert not files.paths


def test_standalone_record_preserves_sdk_sources_and_api_target_without_loading_cli_creds(monkeypatch):
    import json
    from fulcra_api.cli import utils
    from fulcra_gmail.collect_relay import _CollectSDK
    monkeypatch.setattr(utils, "load_creds", lambda: pytest.fail("CLI credentials reached"))
    files = MemoryFiles()
    files.session = SimpleNamespace(_token="synthetic-captured-token")
    calls = []
    files._request = lambda method, path, **kw: calls.append((method, path, kw)) or (200, {}, b'{"upload_id":"synthetic-upload"}')
    sdk = _CollectSDK(files)
    sdk.get_fulcra_userid = lambda: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    target = "MomentAnnotation/11111111-2222-4333-8444-555555555555"
    sdk.resolve_data_type = lambda *a, **kw: [{"id": target, "api_version": "v1alpha1", "recordable": True}]
    sdk.validate_records = lambda **kw: []
    transport = CollectCoordTransport(SimpleNamespace(guard=lambda **kw: None), "synthetic", files=files)
    transport._sdk = sdk
    assert transport.record_write(target, "v1alpha1", "Synthetic companion", "com.fulcra.synthetic", tags=["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"])
    assert len(calls) == 1
    method, path, kw = calls[0]
    assert method == "POST" and path == "/ingest/v1/record/MomentAnnotation"
    assert kw["params"] == {"api_version": "v1alpha1"}
    payload = json.loads(kw["content"])
    assert payload["note"] == "Synthetic companion"
    assert payload["sources"] == ["com.fulcradynamics.cli", "com.fulcra.synthetic", "com.fulcradynamics.annotation.11111111-2222-4333-8444-555555555555"]
    assert payload["tags"] == ["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"]
