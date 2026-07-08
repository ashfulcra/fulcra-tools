"""ATC dashboard (fulcra-agent-atc, task 5).

``dash_data`` is a pure fold (headroom + report tier-mix/headline + map version +
generated_at). ``serve`` / ``make_server`` stand up a stdlib ThreadingHTTPServer
bound to 127.0.0.1 that answers GET ``/`` (one self-contained HTML gauge page),
GET ``/data.json`` (the fold as JSON), and 404s everything else.

The server tests bind an EPHEMERAL port (port 0) in a background thread, drive it
with urllib, then shut it down cleanly — no real network, no fixed port.
"""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from coord_engine import atc, atc_dash, cli
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)

FRONTIER_ACCT = {"id": "amax", "harnesses": ["claude-code"],
                 "windows": [{"hours": 5, "cap": 800}]}


def _accts(*a):
    return {"accounts": list(a), "tiers": {}}


def _sh(tier, age_h=1, *, account="amax", units=0, throttled=False):
    return {"account": account, "ts": NOW - timedelta(hours=age_h),
            "tier": tier, "units": units, "throttled": throttled}


# --- dash_data fold ----------------------------------------------------------

def test_dash_data_shape():
    d = atc.__dict__  # noqa: F841 (readability: dash_data lives in atc_dash)
    out = atc_dash.dash_data(_accts(FRONTIER_ACCT), [_sh("frontier", units=100)],
                             team="fulcra", models={"map_version": "mv1", "models": {}},
                             now=NOW)
    assert set(out) == {"headroom", "tier_mix", "headline", "map_version", "generated_at"}
    assert isinstance(out["headroom"], list)
    assert isinstance(out["tier_mix"], dict)
    assert isinstance(out["headline"], str)
    assert out["map_version"] == "mv1"
    assert isinstance(out["generated_at"], str) and out["generated_at"].startswith("2026-07-08")


def test_dash_data_is_json_serialisable():
    out = atc_dash.dash_data(_accts(FRONTIER_ACCT), [_sh("frontier", units=100)],
                             team="fulcra", now=NOW)
    # round-trips with no datetime/tuple leakage
    assert json.loads(json.dumps(out)) == out


def test_dash_data_headroom_matches_headroom_fold():
    shards = [_sh("frontier", units=200)]
    out = atc_dash.dash_data(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    expect = atc.headroom([FRONTIER_ACCT], shards, NOW)
    assert out["headroom"] == expect
    assert out["headroom"][0]["headroom"] == 600 and out["headroom"][0]["pct"] == 75.0


def test_dash_data_tier_mix_from_report_fold():
    shards = [_sh("frontier"), _sh("cheap"), _sh("cheap"), _sh("cheap")]
    out = atc_dash.dash_data(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    # 1 frontier / 3 cheap of 4 total
    assert out["tier_mix"] == {"frontier": 25, "cheap": 75}


def test_dash_data_headline_string_when_frontier_declared():
    shards = [_sh("cheap", units=400), _sh("frontier", units=100)]
    out = atc_dash.dash_data(_accts(FRONTIER_ACCT), shards, team="fulcra", now=NOW)
    assert "frontier" in out["headline"] and out["headline"] != ""


def test_dash_data_headline_na_without_frontier_account():
    plain = {"id": "amax", "harnesses": ["claude-code"], "windows": []}
    out = atc_dash.dash_data(_accts(plain), [_sh("cheap", units=50)],
                             team="fulcra", now=NOW)
    assert "n/a" in out["headline"].lower()


def test_dash_data_empty_ledger_never_crashes():
    out = atc_dash.dash_data(_accts(), [], team="fulcra", now=NOW)
    assert out["tier_mix"] == {} and isinstance(out["headline"], str)


# --- server (ephemeral port, background thread) ------------------------------

def _run(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read().decode(), r.headers.get("Content-Type", "")


def test_data_json_matches_fold():
    data = atc_dash.dash_data(_accts(FRONTIER_ACCT), [_sh("frontier", units=200)],
                              team="fulcra", now=NOW)
    srv = atc_dash.make_server("127.0.0.1", 0, lambda: data)
    port = srv.server_address[1]
    t = _run(srv)
    try:
        status, body, ctype = _get(port, "/data.json")
        assert status == 200
        assert "json" in ctype
        assert json.loads(body) == data
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)
        assert not t.is_alive()  # clean shutdown


def test_root_serves_selfcontained_html_no_external_urls():
    srv = atc_dash.make_server("127.0.0.1", 0, lambda: {})
    port = srv.server_address[1]
    t = _run(srv)
    try:
        status, body, ctype = _get(port, "/")
        assert status == 200 and "html" in ctype
        assert "<html" in body.lower()
        assert "data.json" in body            # the page fetches the fold
        assert "http://" not in body          # ZERO external scheme in the page
        assert "https://" not in body
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


def test_page_escapes_bus_derived_strings():
    # A hostile tier/account string arrives verbatim over /data.json (the fold
    # does not sanitise — that's the browser's job), and the page MUST escape it
    # before it reaches innerHTML.
    hostile = '<img src=x onerror=alert(1)>'
    data = {"headroom": [{"account": hostile, "window_hours": 5, "headroom": 1,
                          "cap": 2, "pct": 50, "throttled": False}],
            "tier_mix": {hostile: 50}, "headline": "ok",
            "map_version": None, "generated_at": "2026-07-08T00:00:00Z"}
    srv = atc_dash.make_server("127.0.0.1", 0, lambda: data)
    port = srv.server_address[1]
    t = _run(srv)
    try:
        # the raw hostile string is present in the JSON payload...
        _, jbody, _ = _get(port, "/data.json")
        assert hostile in jbody
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)
    # ...but the page defines esc() and routes every bus-derived interpolation
    # site through it (source-level assertion — the page is a static string).
    page = atc_dash.PAGE
    assert "function esc(" in page
    for site in ("esc(r.account)", "esc(r.window_hours)", "esc(r.headroom)",
                 "esc(r.cap)", "esc(r.pct)", "esc(k)", "esc(tm[k])"):
        assert site in page, f"missing escaped interpolation: {site}"
    # the raw (unescaped) label/right interpolations must be gone
    assert "r.account + " not in page
    assert "label: k," not in page


def test_unknown_path_404s():
    srv = atc_dash.make_server("127.0.0.1", 0, lambda: {})
    port = srv.server_address[1]
    t = _run(srv)
    try:
        try:
            _get(port, "/nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


def test_binds_loopback_only():
    srv = atc_dash.make_server("127.0.0.1", 0, lambda: {})
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_data_fn_error_does_not_crash_server():
    def boom():
        raise RuntimeError("fold blew up")
    srv = atc_dash.make_server("127.0.0.1", 0, boom)
    port = srv.server_address[1]
    t = _run(srv)
    try:
        try:
            _get(port, "/data.json")
            assert False, "expected 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            # body must be the generic string — the exception text ("fold blew
            # up") can reflect bus-derived data and must never leak.
            body = json.loads(e.read().decode("utf-8"))
            assert body == {"error": "internal error"}
            assert "fold blew up" not in json.dumps(body)
        # server still alive & serving after the failed request
        status, _, _ = _get(port, "/")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


# --- CLI wiring --------------------------------------------------------------

def test_dash_cli_wires_serve_foreground(monkeypatch, capsys):
    captured = {}

    def fake_serve(team, host="127.0.0.1", port=8787, *, data_fn=None):
        captured.update(team=team, host=host, port=port)
        # exercise the data_fn closure so we know it folds live transport data
        captured["data"] = data_fn()

    monkeypatch.setattr(cli.atc_dash, "serve", fake_serve)
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json",
          json.dumps({"accounts": [FRONTIER_ACCT], "tiers": {}}))
    rc = cli.main(["dash", "fulcra"], transport=t)
    assert rc == 0
    assert captured["team"] == "fulcra"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8787          # default
    assert set(captured["data"]) == {"headroom", "tier_mix", "headline",
                                     "map_version", "generated_at"}


def test_atc_dash_subgroup_alias_wires_same_handler(monkeypatch):
    # spec says `atc dash`; it must resolve to the same handler as top-level
    # `dash` (top-level kept working — asserted by the tests above).
    captured = {}
    monkeypatch.setattr(cli.atc_dash, "serve",
                        lambda team, host="127.0.0.1", port=8787, *, data_fn=None:
                        captured.update(team=team, port=port))
    t = FakeTransport()
    t.put("team/fulcra/atc/accounts.json",
          json.dumps({"accounts": [FRONTIER_ACCT], "tiers": {}}))
    rc = cli.main(["atc", "dash", "fulcra", "--port", "9100"], transport=t)
    assert rc == 0
    assert captured["team"] == "fulcra" and captured["port"] == 9100


def test_dash_cli_custom_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.atc_dash, "serve",
                        lambda team, host="127.0.0.1", port=8787, *, data_fn=None:
                        captured.update(port=port))
    t = FakeTransport()
    rc = cli.main(["dash", "fulcra", "--port", "9001"], transport=t)
    assert rc == 0 and captured["port"] == 9001


def test_dash_cli_has_no_host_flag():
    # --host is deliberately NOT exposed; loopback bind is enforced at the CLI.
    t = FakeTransport()
    try:
        cli.main(["dash", "fulcra", "--host", "0.0.0.0"], transport=t)
        assert False, "--host must not be a recognised flag"
    except SystemExit as e:
        assert e.code == 2  # argparse rejects the unknown flag
