"""ATC localhost dashboard (fulcra-agent-atc, task 5).

A gauge page, not an app. ``dash_data`` is a pure fold — headroom rows, the
report fold's tier-mix + headline, the map version, and a generated-at stamp —
JSON-serialisable so the browser can poll it. ``serve`` stands up a stdlib
``ThreadingHTTPServer`` bound to loopback ONLY that answers:

  * ``GET /``          -> one self-contained HTML page (``PAGE`` below): inline
                          CSS/JS, ZERO external URLs, pure-CSS gauge bars, a
                          ``setInterval(fetch('/data.json'), 30000)`` refresh.
  * ``GET /data.json`` -> ``dash_data`` as JSON (recomputed per request via the
                          injected ``data_fn``, so the poll shows live headroom).
  * anything else      -> 404.

Never crashes: a failing ``data_fn`` yields a 500 with a JSON error body and the
server keeps serving. Bind is 127.0.0.1 only — the ``host`` param exists for the
ephemeral-port tests; no ``--host`` flag is exposed at the CLI.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .atc import demotions, headroom, report_fold

logger = logging.getLogger("coord_engine.atc_dash")

__all__ = ["dash_data", "make_server", "serve", "PAGE"]


def dash_data(accounts: dict[str, Any], shards: list[dict[str, Any]], *,
              team: str, models: Optional[dict[str, Any]] = None,
              days: int = 7, now: Optional[datetime] = None) -> dict[str, Any]:
    """Fold the ledger into the dashboard payload (pure over rows + clock).

    Reuses the task-4 ``report_fold`` for the tier-mix and headline rather than
    re-deriving them, and the ``headroom`` fold for the per-window gauges.
    Returns a JSON-serialisable dict::

        {"headroom": [...], "tier_mix": {tier: pct}, "demotions": [...],
         "headline": str, "map_version": str|None, "generated_at": iso}

    The ``demotions`` list reuses the exact ``{model, task_class, bad, of}``
    shape ``headroom --json`` emits (folded from the same ``demotions`` map), so
    the page can show the active demotions the SKILL doc promises.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    accounts_list = accounts.get("accounts") or []
    hrows = headroom(accounts_list, shards, now)
    demo_map = demotions(shards)
    rep = report_fold(accounts, shards, team=team, demotions=demo_map,
                      models=models, days=days, now=now)
    demo = [{"model": m, "task_class": tc, "bad": v["bad"], "of": v["of"]}
            for (m, tc), v in sorted(demo_map.items())]
    # tier-mix as {name: pct} — a flat dict is what the gauge page renders; the
    # counts already live in the report verb for anyone who needs them.
    tier_mix = {t["tier"]: t["pct"] for t in rep["tiers"]}
    h = rep["headline"]
    if h["value"] is None:
        headline = "n/a — no frontier account declared"
    else:
        headline = (f"~{h['value']:.1f} frontier window-days preserved "
                    "(below-frontier units / frontier 5h cap)")
    return {
        "headroom": hrows,
        "tier_mix": tier_mix,
        "demotions": demo,
        "headline": headline,
        "map_version": (models or {}).get("map_version"),
        "generated_at": now.isoformat().replace("+00:00", "Z"),
    }


# --- HTTP server -------------------------------------------------------------

class _DashServer(ThreadingHTTPServer):
    daemon_threads = True  # worker threads never block a clean shutdown
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int],
                 data_fn: Callable[[], dict[str, Any]]):
        super().__init__(address, _DashHandler)
        self.data_fn = data_fn


class _DashHandler(BaseHTTPRequestHandler):
    server_version = "coord-atc-dash/1"

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("client hung up before response completed")

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler contract)
        path = self.path.split("?", 1)[0]
        logger.debug("GET %s", path)
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/data.json":
            try:
                body = json.dumps(self.server.data_fn()).encode("utf-8")  # type: ignore[attr-defined]
            except Exception:
                # never crash the server on a bad fold — surface it as a 500.
                # The body is deliberately generic: exception text can reflect
                # bus-derived data, so it must not leak into the response.
                logger.exception("dash data_fn failed")
                err = json.dumps({"error": "internal error"}).encode("utf-8")
                self._send(500, "application/json", err)
                return
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        # route stdlib's stderr access log into our logger at debug level so a
        # foreground `atc dash` isn't spammed with per-request lines.
        logger.debug("%s - %s", self.address_string(), fmt % args)


def make_server(host: str, port: int,
                data_fn: Callable[[], dict[str, Any]]) -> _DashServer:
    """Build (but do not start) the dashboard server. Test seam: bind ``port=0``
    for an ephemeral port and drive ``serve_forever`` in a thread."""
    return _DashServer((host, port), data_fn)


def serve(team: str, host: str = "127.0.0.1", port: int = 8787, *,
          data_fn: Optional[Callable[[], dict[str, Any]]] = None) -> None:
    """Serve the dashboard in the FOREGROUND until interrupted.

    ``host`` defaults to loopback and the CLI never overrides it; the param
    exists so tests can bind an ephemeral port. ``data_fn`` supplies the live
    ``dash_data`` payload per request — the CLI wires a transport-reading
    closure; a missing one degrades to an empty payload rather than crashing.
    """
    if data_fn is None:
        data_fn = lambda: {}  # noqa: E731
    srv = make_server(host, port, data_fn)
    bound_host, bound_port = srv.server_address[0], srv.server_address[1]
    logger.info("atc dash serving on %s:%s (team=%s)", bound_host, bound_port, team)
    print(f"serving http://{bound_host}:{bound_port} (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("atc dash interrupted; shutting down")
    finally:
        srv.server_close()


# --- the page (ONE self-contained string; inline CSS/JS; no external URLs) ---
# Kept small on purpose: a gauge page, not an app. NO http:// or https:// may
# appear in this string (tested) — no CDN, no SVG xmlns, no external anything.

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ATC dashboard</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 15px/1.45 system-ui, -apple-system, sans-serif;
         background: Canvas; color: CanvasText; }
  header { padding: 16px 20px; border-bottom: 1px solid GrayText; }
  h1 { margin: 0; font-size: 18px; }
  .sub { color: GrayText; font-size: 13px; margin-top: 4px; }
  main { padding: 20px; max-width: 760px; }
  section { margin-bottom: 28px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em;
       color: GrayText; margin: 0 0 12px; }
  .headline { font-size: 16px; font-weight: 600; }
  .gauge { margin-bottom: 12px; }
  .gauge .lbl { display: flex; justify-content: space-between;
                font-size: 13px; margin-bottom: 4px; }
  .track { height: 14px; border-radius: 7px; background: rgba(128,128,128,.25);
           overflow: hidden; }
  .fill { height: 100%; border-radius: 7px; background: #2b8a3e;
          transition: width .3s ease; }
  .fill.warn { background: #e8590c; }
  .fill.crit { background: #c92a2a; }
  .throttled { color: #c92a2a; font-weight: 600; }
  .demo { font-size: 13px; margin-bottom: 6px; }
  .demo .bad { color: #c92a2a; font-weight: 600; }
  .empty { color: GrayText; font-style: italic; }
  footer { padding: 12px 20px; color: GrayText; font-size: 12px;
           border-top: 1px solid GrayText; }
</style>
</head>
<body>
<header>
  <h1>ATC &mdash; cross-subscription cap ledger</h1>
  <div class="sub">all figures are estimates from self-reported units and operator-declared caps</div>
</header>
<main>
  <section>
    <h2>Headline</h2>
    <div class="headline" id="headline">&hellip;</div>
  </section>
  <section>
    <h2>Account headroom</h2>
    <div id="headroom"><div class="empty">loading&hellip;</div></div>
  </section>
  <section>
    <h2>Dispatch tier mix (last 7d)</h2>
    <div id="tiermix"><div class="empty">loading&hellip;</div></div>
  </section>
  <section>
    <h2>Active demotions</h2>
    <div id="demotions"><div class="empty">loading&hellip;</div></div>
  </section>
</main>
<footer id="foot">map &mdash; &middot; updated &mdash;</footer>
<script>
  // Every bus-derived string (account labels, tier keys, right-hand texts) is
  // built into innerHTML below, so it MUST pass through esc() first — a hostile
  // account id or tier name like `<img src=x onerror=...>` is otherwise live.
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function bar(pct, extra) {
    var cls = pct < 15 ? "crit" : pct < 40 ? "warn" : "";
    var lbl = extra.label, right = extra.right;
    return '<div class="gauge"><div class="lbl"><span>' + lbl +
      '</span><span>' + right + '</span></div><div class="track">' +
      '<div class="fill ' + cls + '" style="width:' + Math.max(0, Math.min(100, pct)) +
      '%"></div></div></div>';
  }
  function render(d) {
    document.getElementById("headline").textContent = d.headline || "—";
    var hr = d.headroom || [];
    document.getElementById("headroom").innerHTML = hr.length ? hr.map(function (r) {
      var t = r.throttled ? ' <span class="throttled">THROTTLED</span>' : "";
      return bar(r.pct, { label: esc(r.account) + " &middot; " + esc(r.window_hours) + "h" + t,
        right: esc(r.headroom) + "/" + esc(r.cap) + " (" + esc(r.pct) + "%)" });
    }).join("") : '<div class="empty">no accounts declared</div>';
    var tm = d.tier_mix || {};
    var keys = Object.keys(tm);
    document.getElementById("tiermix").innerHTML = keys.length ? keys.map(function (k) {
      return bar(tm[k], { label: esc(k), right: esc(tm[k]) + "%" });
    }).join("") : '<div class="empty">no dispatches in window</div>';
    var dm = d.demotions || [];
    document.getElementById("demotions").innerHTML = dm.length ? dm.map(function (x) {
      return '<div class="demo">' + esc(x.model) + " &mdash; " + esc(x.task_class) +
        ' &mdash; <span class="bad">' + esc(x.bad) + "/" + esc(x.of) + " bad</span></div>";
    }).join("") : '<div class="empty">none</div>';
    document.getElementById("foot").textContent =
      "map " + (d.map_version || "—") + " · updated " + (d.generated_at || "—");
  }
  function tick() {
    fetch("/data.json").then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        document.getElementById("foot").textContent = "data fetch failed — retrying";
      });
  }
  tick();
  setInterval(tick, 30000);
</script>
</body>
</html>
"""
