"""collect_plugin orchestration: definition resolution, dedup, ingest wiring.

Uses a REAL ``RunContext`` (so the per-measure preset-slot definition
resolution is exercised against the actual ``resolved_definition_id``) with
in-memory fakes for the daemon seams, and monkeypatches the network edges
(``fetch_api`` / ``post_records``).
"""
from __future__ import annotations

import logging
import types
from datetime import datetime, timezone

from fulcra_collect.plugin import RunContext
from fulcra_purpleair import collect_plugin
from fulcra_purpleair.definitions import METRICS
from fulcra_purpleair.models import Reading


class _FakeAdapter:
    """Stands in for the worker's definition adapter: every name is new, so
    each measure creates a distinct definition id."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def list_definitions(self, *, name: str) -> list[dict]:
        return []

    def create_definition(self, *, name: str, **spec) -> dict:
        self.created.append(name)
        return {"id": f"def::{name}"}

    def definition_exists(self, def_id: str) -> bool:
        return True


def _make_ctx(*, config: dict, credentials: dict | None = None, factory=_FakeAdapter) -> RunContext:
    kv: dict = {}
    claimed: set[str] = set()

    def claim(keys: set[str]) -> bool:
        if keys & claimed:
            return False
        claimed.update(keys)
        return True

    ctx = RunContext(
        plugin_id="purpleair",
        config=config,
        credentials=credentials or {},
        state=types.SimpleNamespace(definition_id=None),
        log=logging.getLogger("test.purpleair"),
        _emit=lambda event: kv.setdefault("_events", []).append(event),
        _fulcra_client_factory=factory,
        _claim_dedup_keys=claim,
        _unclaim_dedup_keys=lambda keys: claimed.difference_update(keys),
        _plugin_kv_get=lambda key, default: kv.get(key, default),
        _plugin_kv_set=lambda key, value: kv.__setitem__(key, value),
        _plugin_kv_update=lambda key, fn, default: None,
        _plugin_kv_delete=lambda key: kv.pop(key, None) is not None,
    )
    ctx._kv = kv  # test handle
    ctx._claimed = claimed  # test handle
    return ctx


def _reading(**kw) -> Reading:
    base = dict(
        sensor_id="90210",
        observed_at=datetime(2026, 7, 22, 20, 0, 0, tzinfo=timezone.utc),
        pm2_5=8.3, pm10=9.1, aqi=35,
        temperature_f=72.0, humidity=45.0, pressure=1013.2,
    )
    base.update(kw)
    return Reading(**base)


def test_resolve_definition_ids_yields_six_distinct_and_caches():
    ctx = _make_ctx(config={})
    ids = collect_plugin._resolve_definition_ids(ctx)
    assert len(ids) == 6
    assert len(set(ids.values())) == 6           # one distinct def per measure
    assert ctx._kv["definition_ids"] == ids       # written back to KV
    # A second call reuses the cache (validated live) — no new creates.
    ids2 = collect_plugin._resolve_definition_ids(ctx)
    assert ids2 == ids


class _SwitchedAccountAdapter:
    """Every cached id is absent on this account (an account switch), and each
    name resolves to a brand-new id — so a stale cache MUST be re-resolved,
    never trusted."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def list_definitions(self, *, name: str) -> list[dict]:
        return []

    def create_definition(self, *, name: str, **spec) -> dict:
        self.created.append(name)
        return {"id": f"new::{name}"}

    def definition_exists(self, def_id: str) -> bool:
        return False


def test_resolve_definition_ids_reresolves_stale_ids_after_account_switch():
    ctx = _make_ctx(config={}, factory=_SwitchedAccountAdapter)
    # Seed a warm cache of ids from the *previous* account.
    ctx._kv["definition_ids"] = {m.key: f"old::{m.key}" for m in METRICS}

    ids = collect_plugin._resolve_definition_ids(ctx)
    # None of the stale ids survive; every measure re-resolved on the new account.
    assert all(not v.startswith("old::") for v in ids.values())
    assert all(v.startswith("new::") for v in ids.values())
    assert len(set(ids.values())) == 6
    assert ctx._kv["definition_ids"] == ids


def test_run_writes_all_measures_and_advances_cursor(monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda idx, key, **kw: _reading())
    monkeypatch.setattr(collect_plugin, "post_records",
                        lambda client, records: posted.append(list(records)))

    ctx = _make_ctx(config={"mode": "api", "sensor_index": "90210"},
                    credentials={"api_key": "k"})
    collect_plugin.run(ctx)

    assert len(posted) == 1
    assert len(posted[0]) == 6
    assert ctx._kv["last_observed"] == _reading().observed_at.isoformat()


def test_run_skips_already_claimed_reading(monkeypatch):
    posted: list[list[dict]] = []
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda idx, key, **kw: _reading())
    monkeypatch.setattr(collect_plugin, "post_records",
                        lambda client, records: posted.append(list(records)))

    ctx = _make_ctx(config={"mode": "api", "sensor_index": "90210"},
                    credentials={"api_key": "k"})
    collect_plugin.run(ctx)   # first: writes
    collect_plugin.run(ctx)   # second: same sample -> claim rejects
    assert len(posted) == 1


def test_run_only_writes_present_measures(monkeypatch):
    posted: list[list[dict]] = []
    partial = _reading(pm10=None, pressure=None, humidity=None)
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda idx, key, **kw: partial)
    monkeypatch.setattr(collect_plugin, "post_records",
                        lambda client, records: posted.append(list(records)))

    ctx = _make_ctx(config={"mode": "api", "sensor_index": "90210"},
                    credentials={"api_key": "k"})
    collect_plugin.run(ctx)
    assert len(posted[0]) == 3   # pm2_5, aqi, temperature


def test_run_unclaims_on_post_failure(monkeypatch):
    def boom(client, records):
        raise RuntimeError("ingest down")

    monkeypatch.setattr(collect_plugin, "fetch_api", lambda idx, key, **kw: _reading())
    monkeypatch.setattr(collect_plugin, "post_records", boom)

    ctx = _make_ctx(config={"mode": "api", "sensor_index": "90210"},
                    credentials={"api_key": "k"})
    try:
        collect_plugin.run(ctx)
    except RuntimeError:
        pass
    # Claim released, so the reading is retryable rather than lost forever.
    assert ctx._claimed == set()


def test_run_local_mode_fetches_each_ip(monkeypatch):
    posted: list[list[dict]] = []
    seen_ips: list[str] = []

    def fake_local(ip, **kw):
        seen_ips.append(ip)
        return _reading(sensor_id=ip)

    monkeypatch.setattr(collect_plugin, "fetch_local", fake_local)
    monkeypatch.setattr(collect_plugin, "post_records",
                        lambda client, records: posted.append(list(records)))

    ctx = _make_ctx(config={"mode": "local", "sensor_ips": "10.0.0.5, 10.0.0.6"})
    collect_plugin.run(ctx)
    assert seen_ips == ["10.0.0.5", "10.0.0.6"]
    assert len(posted) == 2


def test_unknown_mode_raises():
    import pytest
    ctx = _make_ctx(config={"mode": "banana"})
    with pytest.raises(RuntimeError, match="unknown mode"):
        collect_plugin.run(ctx)
