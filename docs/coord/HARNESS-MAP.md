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

## The wake mechanism, per harness — what actually RE-ENTERS the agent

A harness row is not complete until it names the thing that runs the model again.
Everything else — schedules, notifications, files on disk — is plumbing *around*
that mechanism, and plumbing that does not end in the model running is not a wake.

| # | Harness | THE WAKE (runs the model) | Router adapter | Notes / what does NOT wake it |
|---|---------|---------------------------|----------------|-------------------------------|
| 1 | Claude Code, local desktop | **session-level recurring prompt** (the harness's own scheduled-prompt tool) | `queued-wake-file` collected by that loop; `macos-notify` = escalation only | **`launchd` cannot wake this harness** — it runs shell, not the model. `claude -p` invokes headlessly ONLY where the CLI is authenticated; unauthenticated it fails after minutes and reads as a hang. Session loop dies with the session — this harness has NO overnight autonomy without headless auth or relocation. |
| 2 | Claude Code, remote/CCR | **CCR Routine** (cron, 24/7) | `managed-agents-message` | Durable: survives the operator closing their machine. The reason cloud agents out-produce desktop ones. |
| 3 | Codex CLI | **codex thread heartbeat** | `codex-exec-resume` (`thread_id`) | Tightest cadence in the fleet (1–15 min); pull-based, so directed wakes are an optimisation, not a dependency. |
| 4 | OpenClaw | **OpenClaw heartbeat** | `openclaw-post` (`endpoint_name`) | Needs a named webhook endpoint configured, or the route is inert. |
| 5 | GitHub Actions CI | n/a — event-triggered, no resident agent | none | Nothing to wake. |
| 6 | Headless heartbeats (launchd/cron) | **the schedule IS the agent** | none | No model to re-enter; a notifying adapter is meaningless here. |
| 7 | ChatGPT facade | **the inbound request** | none | Per-request; no standing agent. |
| 8 | Claude mobile/desktop remote control | **the operator** | none | Best-effort transport, never a dependency. |

**Two rules that fall out of this table.**

1. **Adapter success is not agent execution — and only TWO adapters prove it.**
   `managed-agents-message` re-enters a session directly, and `codex-exec-resume`
   invokes `codex exec resume <thread-id>` to re-enter the exact persisted thread.
   `queued-wake-file` and `routine-align` are INDIRECT: they land a nudge or align
   a schedule, and the wake completes only if that independent consumer (session
   loop, Routine) exists and is verified running — `align_routine` records
   `no_session_created: true` in its own result, and a wake file is consumed only
   at a later `SessionStart`. `macos-notify` terminates in a human's eyeballs and
   is never a wake. An agent whose only route is notifying, or whose indirect route
   has no live consumer, is **un-wakeable while the router reports clean
   deliveries.**

   Classify by the adapter's CONTRACT, not by whether it is wired up here — those
   are different questions and the second one is loud enough to drown the first.
   Separately and independently: three of the five host-local adapters
   (`codex-exec-resume`, `openclaw-post`, and `opencode-wake` from PR 482)
   currently lack either a script or a registration, so their routes validate and
   never execute. `codex-exec-resume` sits in both lists — a DIRECT adapter that
   cannot execute today — which is exactly why the two questions need separate
   answers.
2. **Never retire a wake without naming and proving its replacement.**
   coord-maintainer — the maintainer of this system — deleted its standing session
   loop on 2026-07-24 to save budget, and replaced it over two weeks with a
   notifier that woke a human, a headless invoke that could not authenticate, and
   a `--peek` read that never consumed. Three replacements, none of which ran the
   agent, each reporting success. It went unnoticed for two weeks because every
   layer's own evidence looked healthy. Prove the replacement RUNS THE AGENT
   before removing what worked.

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
    directive open forever (fixed in v1.6.5: fails loud). Same family as
    walls 4 and 5: *the absence of an error is not success.*
11. **Cloud sessions are repo-scoped; account-level GitHub access does not
    reach them** (2). A cloud session's GitHub credential covers exactly the
    repos attached when the session started — granting the *account* access to
    another repo changes nothing mid-session, and cross-owner `add_repo` is
    unsupported (v1). Hit twice on 2026-07-22: a handed-off import stalled
    against an empty target repo, and a worker's push returned 403 until its
    session was restarted. Workarounds, in preference order: the operator
    starts (or restarts) the session **with the repo as an initial source**;
    mirror-push the content into a repo that IS in scope; or work a
    **same-owner fork**. Plan this in the parking doc's operator pre-flight
    (fulcra-agent-continuity, "Parking for a successor") — discovering it
    serially costs a human round-trip per failed attempt.
12. **`setsid`-detached processes are reaped when the tool call returns; the
    harness's own background mechanism is not** (2). A long job started with
    `setsid bash -c '… > out 2>&1' &` dies when the shell that launched it
    exits — and dies *silently*: the wrapper's own trailing
    `echo "RC=$?"` never runs, so the output file is left at **zero bytes**
    with no error anywhere. The same command run through the harness's
    background facility completes normally. Verified twice on 2026-08-06/07
    with `coord-engine health fulcra` (~25 min on a 1.2s/op transport): two
    `setsid` runs → 0 bytes, no RC line, no process; one harness-background
    run → full census, `rc 0`.
    This is the same family as BOOTSTRAP's *"never `& disown` a leg inside a
    launchd job"* — a supervisor reaping the children of an exited parent — and
    it fails the same way: it survives an interactive test and vanishes in
    production. **An empty output file is the signature; do not read it as
    "the job found nothing."** The two failure modes are distinguishable:
    a job you killed with a short `timeout` exits **rc 143** with partial
    output, while a reaped job leaves **zero bytes and no exit line at all**.
    Long legs also exceed the 10-minute foreground tool ceiling, so background
    is not optional for them — use the harness's, and always write a trailing
    RC line so a truncated run is detectable rather than ambiguous.

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

13. **A gated harness refuses to EXECUTE a downloaded install script, and that
    refusal is correct** (codex; verified live 2026-08-07). The sanctioned
    adoption flow is `download adopt-latest.sh` then `bash /tmp/a.sh <agent>`.
    On the codex harness the approval classifier rejects the second step
    because *a downloaded script can install packages and persistently modify
    the environment*. This is not a bug to engineer past — it is an
    operator-level control, and an agent that routes around it has broken
    something more important than its own currency.
    **What the classifier objects to is OPACITY, not the operations.** The two
    installs are fine; an opaque blob doing arbitrary things is not. So the
    supported path is to READ the authority values (`PIN=`, `VER=`, `COMMON=`)
    out of the script — a file read, nothing to refuse — and run the two
    `uv tool install` commands inline, where the whole command line is visible
    to the approval layer. Never hardcode the pin into the recipe: read it at
    run time, or it rots at the next pin move like every other copied pin has.
    Then perform the three checks the script's claim gate performs (bus-v3 verb
    resolves; `doctor` prints a pin-currency line naming the PIN; `fulcra_common`
    imports **in the engine's own interpreter**) and only then send the claim —
    file NO claim if any check fails. Recipe:
    `team/fulcra/_coord/bus-v3/ADOPT-WHEN-GATED.md`, linked from the head of
    `adopt-latest.sh` so it is found at the moment of refusal rather than
    searched for afterwards.
    If the literal commands are ALSO refused, that is a genuine operator unlock:
    say so and stop. A blocked agent is not blocked on work — a stale pin still
    reads the bus — so it is currency, not capability, and it should keep
    working while it reports the block.

## Change log

- 2026-07-14: initial map (coord-boss), from the 07-11..07-14 incident record.
- 2026-07-22: wall 11 (cloud repo scoping), from Webster's handoff
  retrospective + the Fabio session restart (BUS-79).
- 2026-08-07: wall 12 (setsid reaping), from two silent `health` census
  failures during a coord-boss watchdog sweep — both zero-byte, neither
  reported by anything.
- 2026-08-07: wall 13 (gated harness refuses the install script), from
  codex-coder hitting it on the pin move to e1880da9 and adopting via the
  literal-commands path instead.
