# Harness map — where fulcra agents run, and what breaks where

First deliverable of the surface-monitoring backlog item (2026-07-11, coord-boss):
before we can monitor every fulcra surface in every environment, we need the map
of environments. Every row below is a harness an agent has actually run in on
this team's bus; every wall is an incident that actually happened, with the fix
or workaround that closed it. Keep this current: when a new harness joins the
fleet or a new wall is hit, add it here in the same PR as the fix.

## The harnesses

| # | Harness | Fleet examples | Traits that matter |
|---|---------|----------------|--------------------|
| 1 | Claude Code, local macOS | coord-maintainer, fulcra-primitives-maintainer (Mac); prefs_maintainer (Workbook) | Full CLI + browser auth, direct git (tags OK), launchd access, persistent disk |
| 2 | Claude Code, remote/web container | coord-boss (this doc's author) | TLS-intercepting proxy, egress allowlist, ephemeral disk, container restarts kill background loops, git **gateway** (no `gh`; GitHub via MCP), 24h token loop |
| 3 | Codex CLI (OpenAI) | codex-reviewer, codex-coder | Tick-based loops, separate account/limits (ATC-tracked), own sandbox quirks |
| 4 | OpenClaw | Arc (openclaw:discord:*) | Discord-fronted, long-lived, different skill loading |
| 5 | GitHub Actions CI | resolve gate (ubuntu), macOS suite | **No Fulcra credentials by design** — test hermeticity is a safety boundary, not a convenience |
| 6 | Headless heartbeats (launchd/cron) | coord-reconcile:* hosts | Restricted PATH, no browser, no human at the keyboard — silent failure is the default failure mode |
| 7 | ChatGPT facade (HTTP) | find-or-create / status endpoints | Per-request process economics (see the loop-2 perf audit) |
| 8 | Claude mobile/desktop remote control | operator-driven sessions | Unreliable delivery (the reason coord-boss exists); treat as best-effort transport, never a dependency |

## The walls (verified incidents × harness)

Each of these was hit live, diagnosed, and closed. The harness column says where
the wall exists — not where it happened first.

1. **Device-flow auth bypasses HTTPS proxies** (harness 2, any proxied CI).
   `fulcra auth login` uses raw `http.client`, ignoring `HTTPS_PROXY`. Filed
   upstream (fulcra-api-python#55). Workaround: proxy-aware urllib device flow +
   refresh-token grant writing `~/.config/fulcra/credentials.json` — documented
   in [GET-ON-THE-BUS.md](GET-ON-THE-BUS.md) §3.
2. **24h token expiry vs long-lived agents** (2, 6). Token refresh shares the
   proxy bug; headless agents must refresh proactively (<2h window) or die at
   the least attended hour. Standard watchdog leg now does this.
3. **The catalog is deletion-blind** (all harnesses). `/data/v1/catalog` reports
   soft-deleted user annotation definitions as `deprecated: false`; ingest
   accepts records against deleted definitions and they render nowhere. Fixed
   client-side with authoritative per-id verification (fulcra-common
   `d87cdc2` / v0.1.1); routed upstream 2026-07-14.
4. **Git gateways reject tag pushes with a misleading success** (2). Branch
   pushes work; `git push origin <tag>` fails ("remote end hung up") yet prints
   "Everything up-to-date". Always verify with `git ls-remote origin
   refs/tags/<tag>`; delegate tag cuts to a harness-1 host.
5. **Silent no-op writers** (2, 6). coord-engine is stdlib-only; without
   `fulcra_common` importable beside it, timeline projection degrades to a
   quiet no-op — this darkened annotations fleet-wide for 6 days. Fixed:
   `--with fulcra-common` install recipe (GET-ON-THE-BUS §"Enable timeline
   projection") + loud warns. Doctrine: a best-effort leg must WARN when its
   backend is absent.
6. **Test suites writing to the production account** (1, historically). ~7,800
   junk timeline moments from fixture runs. Fixed: autouse dummy-token conftest
   (writes 401 and land nowhere) + hermetic stubs. Open hygiene nit: urllib
   writers aren't covered by httpx MockTransport — an unmocked test still makes
   a real (rejected) POST.
7. **File Store read-latency spikes** (worst on 2). Big-team folds (briefing,
   digest) can exceed 2 minutes remotely while writes stay fast. Mitigations:
   `COORD_TRANSPORT_TIMEOUT` on interactive paths (never on listen legs —
   slow-honest beats fast-lying), budget-bounded folds that report "scanned
   N/M" instead of pretending completeness.
8. **Container restarts kill background loops** (2). Listeners and watchers die
   with the container. Standing-watch doctrine: PID-file single-flight listener
   + an out-of-band hourly watchdog (cron/Routine) that re-arms it; never rely
   on one layer.
9. **Cached identifiers outlive the things they identify** (all). Definition-id
   caches pinned `pinned:true, never expires` kept writing to a definition
   deleted 10 days earlier. Doctrine: caches for remote identities need TTLs
   AND an authoritative liveness re-check (see wall 3).
10. **Silent-success on nonexistent targets** (all). `respond` against a
    mistyped/display-title slug records a ghost response and leaves the real
    directive open forever (accepted for v1.6.5: fail loud). Same family as
    walls 4 and 5: *the absence of an error is not success.*

## What "monitoring" should grow into

The pattern in every wall above: **the failure was silent in the harness where
nobody was looking.** The monitoring vision, staged:

1. **Now (cheap):** every heartbeat host runs `doctor` on its cadence; the
   twice-daily digest carries headroom + health lines (shipped). Walls found in
   one harness get regression-tested in CI where possible.
2. **Next:** a canary matrix — one scripted probe per (harness × surface) pair
   that exercises auth, a read fold, a write, and a timeline emit, reporting
   into the bus as presence + a `reports/` shard. ATC can route canary runs to
   whichever account has headroom.
3. **Eventually:** the full surface-monitoring program (the 2026-07-11 backlog
   item): all fulcra surfaces (CLI, lib, MCP, REST, File Store, skills) probed
   from all harnesses above on a cadence, with drift detected against pinned
   baselines (e.g. `docs/specs/fulcra-openapi-digest.txt`).

## Change log

- 2026-07-14: initial map (coord-boss), from the 07-11..07-14 incident record.
