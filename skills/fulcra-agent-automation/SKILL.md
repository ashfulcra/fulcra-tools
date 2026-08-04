---
name: fulcra-agent-automation
description: "Keep a fulcra-agent-teams space healthy unattended: schedule coord-engine reconcile on a heartbeat so the index/views stay healed, and resume structured continuity on cron/heartbeat wake-ups."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "⏱️" } }
---

# Fulcra Agent Automation

> **BUS V3 (2026-07-27, operator-ordered): the listener half of this skill is RETIRED.**
> Agents read their event queue on every wake — one bounded `get-records` query
> ([`docs/coord/BUS-V3.md`](../../docs/coord/BUS-V3.md)) — and run **no resident
> listener**. Do not install new listeners (§2) or listener-tick automation; hosts still
> running one should stand it down. The **heartbeat** (§1: scheduled `reconcile` +
> projection + digest) and the **wake-on-schedule adapters** (§3, repurposed to trigger
> a queue-reading wake rather than a listen tick) remain current. The `listen` verb
> itself was REMOVED from the engine on 2026-08-03 (PR #523); invoking it is now an
> argparse error.

Ties the coord skills together for **unattended** operation. `fulcra-agent-reconcile` heals a team's
index/views, but someone has to run it; this skill **schedules** it, and makes wake-ups
(cron/heartbeat) **resume structured continuity** first. Scheduling is a single, platform-specific action
(not a fold), so this skill is prose + one bundled install script — no engine logic.

**Require consent:** always ask the user before creating any scheduled job or background automation.

## Where to start — the re-entrancy probes

Before installing anything, probe what this host already runs. Enter at the **first probe that
fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`):

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Engine usable? | `coord-engine doctor <team>` | exits 0 and last line is `doctor: healthy` | fix engine/auth first (see fulcra-agent-reconcile) — do NOT install jobs on a broken engine |
| Heartbeat installed? | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.heartbeat.<team>.plist` (macOS) or `crontab -l \| grep coord-engine.heartbeat.<team>` (Linux) | file/line exists | §1 (install the heartbeat) |
| Heartbeat loaded? | `launchctl list \| grep coord-engine.heartbeat.<team>` (macOS only) | a line appears | reinstall via §1 (plist exists but is not loaded — a reboot-era failure mode) |
| Listener installed + loaded? (RETIRED — see banner) | `ls ~/Library/LaunchAgents/com.fulcra.coord-engine.listener.<team>.*.plist` and `launchctl list \| grep coord-engine.listener.<team>.` | **inverted since bus v3**: this probe passing means a retired listener is still running — stand it down: `launchctl bootout gui/$(id -u)/<label> && rm ~/Library/LaunchAgents/<label>.plist` (macOS) or delete its crontab line (Linux); the installer script was removed with the listener stack | nothing — a missing listener is now the healthy state; do NOT enter §2 |
| Views actually fresh? | `coord-engine health <team>` | this host's row shows a recent `last reconcile` | jobs exist but are not ticking — check `log show` / cron mail, then reinstall |
| Claude Code hooks installed? | `ls ~/.claude/fulcra-agent-hooks/` | 3 scripts | §3 (install-claude-code.sh) |
| Codex watch coord-first? | `grep -l "coord watch" ~/.codex/hooks.json` | file matches | §3 (install_codex_watch.py) |
| OpenClaw block present? | `grep "fulcra-agent:begin" <workspace>/HEARTBEAT.md` | one match | §3 (install_openclaw.py) |

All probes pass → nothing to install; re-running any installer is safe anyway (reinstall replaces the
job, never duplicates — CI-tested for the heartbeat's launchd and cron paths; the listener installer
and its test suite were removed with the retired stack). Re-running after this upgrade also MIGRATES any
coord2-era artifacts (old `fulcra-coord2-hooks/` dir, `coord2-watch-<agent>` automation, `coord2 watch`
hooks marker, `fulcra-coord2:begin` fence) to the new names in place — old artifacts removed, zero
orphans (host-simulation-tested in `packages/coord-engine/tests/test_adapter_installers.py`).

## 1. Heartbeat — keep the views healed
Schedule `coord-engine reconcile <team>` on a timer so the index/aggregate never drift, even when no
agent is actively working.

**Bundled installer (macOS launchd / Linux cron):**
```bash
# every 20 minutes (default); creates a LaunchAgent on macOS or a crontab line on Linux
./scripts/install-heartbeat.sh <team> [interval-minutes]
./scripts/install-heartbeat.sh --uninstall <team>
```
It runs `coord-engine reconcile <team>`; needs `coord-engine` + an authenticated `fulcra-api` on PATH.

**OpenClaw / other runtimes:** add a line to your `HEARTBEAT.md` (or a native cron job, per
`fulcra-agent-teams`' automation section) that runs `coord-engine reconcile <team>` on your
chosen cadence. Prefer a longer interval or an external loop over waking the model every tick.

### Projection — task transitions onto your Fulcra timeline (model-free)
The heartbeat can project each task transition (create / pickup / update / complete) onto your Fulcra
timeline as an Agent-Tasks annotation, mechanically, spending no model tokens — and it annotates
transitions made by *any* agent or harness, not just this host. Reconcile already computes the
transitions; projection folds them onto the timeline right after.

Opt in **per team** (default is `off`):
```bash
coord-engine annotate resolution <team> transitions   # turn projection on for this team
coord-engine annotate resolution <team> off           # turn it back off
coord-engine annotate status <team>                    # resolution level + cursor position
```
The level is stored on the bus, so every host's heartbeat reads the same setting. With projection on,
`install-heartbeat.sh` runs `coord-engine annotate project <team>` immediately after each `reconcile` —
it consumes the structured `pending.json` transitions reconcile just wrote (the `log.md` bullets can't
feed the fold — they carry no task_id/kind/ts), emits one annotation per new transition (deterministic
id + cursor, so a re-run or mid-run crash never double-writes), and advances the cursor. Off or absent
⇒ the step no-ops. The heartbeat chain then finishes with `coord-engine digest <team> --store
--emit-timeline`, which keeps the operator's twice-daily digest alive on both surfaces (bus copy +
the 'Agent Tasks — Digest' timeline track) — see the health skill for its semantics.

Multi-host is safe: the typed ingest endpoint **upserts on an explicit record id** (live-verified
2026-07-14 — a same-id re-POST returns 201 and the record count stays 1), and every projected
annotation carries a deterministic id, so two hosts racing `reconcile && annotate project` in the
same window converge on the same records instead of duplicating them. The cursor still matters — it
is what keeps quiet ticks cheap (no re-POSTs) — but it is an efficiency guard, not the only thing
between you and duplicates. Run projection from every heartbeat host.

`resolution` is a **level axis, not a boolean**: `{off, transitions}` are live today; finer levels (tool
calls, I/O, …) are additive later without a config-shape change. Any other value is rejected.

**Projection is the successor to the in-process `fulcra-coord annotations` writer.** Both emit
Agent-Tasks moments for the same transition to a no-dedup endpoint, so running both double-writes the
timeline. Enabling projection therefore requires the legacy annotations writer stay off — which the
standing rule ([`AGENTS.md` → Fulcra platform surface](../../AGENTS.md)) already mandates on every host.
Projection is the sanctioned replacement; do not switch the legacy writer back on to get timeline
annotations — turn projection on instead.

## 2. Awaiting replies — the reply leg is the queue read (listener REMOVED)

**There is no listener.** Replies to an ask (`tell`/`broadcast`/`remind`/`review request`)
arrive as bus v3 events; you await them by reading your event queue on your next
scheduled wake:

```
coord-engine queue <team> --agent <you>
```

The send verbs print exactly that as their breadcrumb — `tell`/`broadcast` echo
`replies: coord-engine queue <team> --agent <you>` and `review request` echoes
`await verdicts: coord-engine queue <team> --agent <me>`.

The `coord-engine listen` verb — the resident diff/notify watcher this section used
to document (inbox/response/verdict id-diffing, `LISTEN DEGRADED` streaks, the
head/tail budgets) — was retired as the wake surface on 2026-07-27 and **removed from
the engine entirely on 2026-08-03 (PR #523)**: running it is an argparse usage error,
its env knobs (`COORD_LISTEN_*`, `COORD_LISTENER_STATE`) are gone, and its durable
`listen-state.json` shards are historical residue (see the takeover note in
GET-ON-THE-BUS §5). The mechanics live in git history, not here.

**A scheduled wake must never die on degradation** (the doctrine outlives the
watcher): a degraded queue read or fold backs off and re-fires on the next tick;
only affirmative delivery ends the wait. Degradation is never interpreted as a
clean queue, and never as permission to stop waking.

The load-bearing **wake** read is the bus v3 queue read
(`coord-engine queue <team> --agent <you>`), with `briefing` as the durable-state fold you run after it.
When a scheduled tick's `queue`/`briefing`/`inbox` degrades, quiet is NOT clear — apply the raw-bus fallback
in §3 (**Degraded briefing → fail-closed raw-bus fallback**): raw-list + direct-read the unacked
directives before reporting, never conclude "no work" off a degraded read.

### Per-platform — pick the leg that matches how the agent runs
- **launchd / cron (unattended host): REMOVED with the retired stack (cleanup slice 1,
  2026-07-28)** — do not install; stand down any survivor per the probe table. The
  bundled installer's scheduled tick, notification/consent-gated wake chain, adaptive
  due-time gate, and `COORD_LISTENER_*` advisory env fields all went with it (git
  history has the details). What replaces it on an unattended host is a plain
  scheduled wake (launchd/cron) running the queue read — see
  [`docs/coord/GET-ON-THE-BUS.md`](../../docs/coord/GET-ON-THE-BUS.md) §6 for the
  crontab recipe, and keep the heartbeat (§1) for reconcile.
- **Every platform (Claude Code live, Codex, headless): the queue read.** These platforms
  once ran resident `listen` variants; the verb no longer exists. The pickup on every
  platform is the bus v3 queue delivery (`coord-engine queue <team> --agent
  <agent>`) on the wake the platform already has (automation tick, scheduled
  job, session start). Under cursor v2, the harness must leave the token
  uncommitted while the agent works and run `queue commit` only after every
  event in the staged batch has a durable terminal classification, supplied as
  one `--result <record-id>=<outcome>` per event. A harness consuming
  `queue --json` parses the single stdout object and switches on `type`:
  `queue-result` (state `DATA`|`CLEAR`) is the whole success surface, and
  every nonzero exit yields one `queue-error` that must be reported with its
  `state` and `error_code` verbatim — the states are not interchangeable:
  `INVALID` (`*-invalid`, `event-id-missing`) is corrupt human-fixable data
  that no retry will clear (surface it to the operator; never delete or
  recreate the named file to "unstick" the read); `UNKNOWN` (`*-read-failed`,
  `window-unknown`, `consume-audit-failed`, `stage-race-unverified`) is a
  transport doubt that backoff-and-retry handles; `INCOMPATIBLE`
  (`engine-incompatible`, `cas-unsupported`, `authority-not-v2`) means
  upgrade or reconfigure, not retry; `ABSENT` (`config-absent`) means the
  bus is not set up; `REFUSED` (`usage`, `results-incomplete`,
  `stale-token`) means the invocation itself was wrong.

For push-capable harnesses and the fleet security contract, see
[`docs/coord/EVENT-DRIVEN-WAKE.md`](../../docs/coord/EVENT-DRIVEN-WAKE.md). The bundled
`wake/openclaw.sh` and `wake/codex.sh` adapters were removed with the listener stack (cleanup
slice 1); directed wakes are now the wake router's job — its adapters are **host-local**
(`$COORD_WAKE_ADAPTER_DIR/<adapter>.sh` on the executor host, e.g. `codex-exec-resume` still using
`codex exec resume <thread-id>` without bypassing approvals or sandboxing), registered per agent in
`_coord/router/config.json`. `wake/macos-notify.sh` remains in-repo as a live router adapter.

**Single-flight remains an efficiency rule, not a correctness assumption.**
The listener-era cursor could lose work when two same-agent wakes overlapped.
Cursor v2 CAS-stages one pending batch: a losing wake reloads and replays the
winner, stale commit tokens cannot advance coverage, and successful commit
retries are idempotent. Still keep one scheduled wake per agent identity and
coalesce overlaps—the second wake adds cost and may duplicate processing even
though it cannot corrupt coverage.

## 3. Harness adapters — lifecycle wiring
Bus v3 queue reads (on each wake) deliver events, but the **lifecycle contract** — resume-on-wake,
snapshot-on-change, park-before-context-loss — is owned by a per-harness adapter that hooks the
platform's own session events. The contract itself (rules 1–4) lives in
[`fulcra-agent-continuity` §The lifecycle contract](../fulcra-agent-continuity/SKILL.md); the adapters
below automate it. Each keys everything on a distinct `coord` marker. Retired
first-generation entries are left inert unless an installer's documented
migration path explicitly recognizes them. All installers are idempotent
(reinstall replaces, never duplicates) and ship an `--uninstall` inverse.

**Tick doctrine (shared by every adapter).** Every adapter keys off the same canonical, **queue-led**
tick — though claude-code's hook renders only the first steps (`continuity resume` + `briefing`) as session
context, leaving the queue read and the verdict-before-ack duty steps to the waking agent:
`continuity resume` → **`queue` (READ your events, process them, and — under cursor v2 — `queue commit`
the staged token only after every event has a durable terminal classification; this is the wake surface,
not `briefing`)** → `briefing` (the durable-state fold — identity, role inboxes, needs-me incl. pending
reviews; it is what the events do NOT cover, never a substitute for the queue read) → for each review request, **slug-exact verdict-before-ack** (write the verdict file, verify
`review status` clears you from `pending_required`, only then ack — never ack bare or against a different
slug) → handle other work → `continuity snapshot` → `usage log` (ATC, when accounts are declared) →
`continuity park` before session end → **report last**: the human-visible summary is the tick's final
output, composed after every command above. Text followed by more tool activity may never render —
"sent" is not "delivered" — so anything that MUST reach a recipient (human or agent) goes on the bus
as a durable artifact (ask, review doc, snapshot), never only in session text. PR/forge feedback
arrives via `briefing` (forge mirror sweeps all three GitHub surfaces) — never hand-roll `gh` polls
in a watch prompt. **These
hooks/prompts/blocks are rendered artifacts, not live references:** after upgrading `fulcra-tools`,
**RE-RUN YOUR ADAPTER INSTALLER** to regenerate them — an un-regenerated hook keeps emitting the
doctrine it was rendered under.

**Degraded briefing → fail-closed raw-bus fallback (doctrine, not optional).** A wake read that
degrades is *absence of a complete answer, never proof of "all clear"* — and the fallback covers
**every** degraded section, not just reviews. A watcher that acts only on what a clean-looking fold
returned can silently drop a live unacked directive.

- **Directives / inbox — the general case.** Every aggregate-backed read (`briefing`, `inbox`,
  `needs-me`, `status`, `board`, `search`) folds the summaries index through the public-read failure
  contract ([`AGENTS.md` → The public-read failure contract](../../AGENTS.md)): when the index/listing
  is UNKNOWN it emits the shared `read-degraded` marker (or `inbox`'s named `inbox-degraded` type) —
  carried in the `--json` result and as a stderr notice — instead of a clean-empty. On ANY such marker
  (or a `briefing` that reports a failed resume / stalled section), the watcher MUST NOT conclude "no
  work": it **raw-lists and direct-reads the unacked directives** — enumerate `team/<team>/task/`
  (`fulcra-api file list`), read each `intent:`/`assignee`-shaped doc naming this agent or a role it
  holds, and act on anything open-and-unacked. Only report a genuinely clear inbox when a *non-degraded*
  read returns empty; a degraded read is reported **degraded**, never "no directives."
- **Reviews — the specific case.** The `briefing`/`needs-me` pending-review fold is **projection-first**:
  it serves the reconcile-built `reviews` section of `_coord/summaries.json` when that section is fresh,
  and discloses which source it used in a trailing `review-source` row — `review fold: projection (as of
  T)` or `review fold: raw scan — <reason>` (stale / incomplete / malformed / unrecognized). A raw-scan
  row is a **loud** fallback, never a silent one; read it as "a reconcile is behind", not as an error in
  your own obligations. The caller's OWN head slugs are raw-tallied on every call either way.
  The raw scan is wall-clock bounded
  (`COORD_REVIEW_FOLD_BUDGET`, default 45s): on a slow transport it stops early and emits a
  `review-fold-degraded` row (`{scanned, total}`, plus `skipped` when a slug's doc or verdict read
  failed) rather than a clean-looking partial. On that row, fall back to a per-slug `review status`
  sweep over the `review/` listing for the unscanned and skipped remainder, and clear those verdicts
  before acking. Full contract: [`docs/coord/BUS-V3.md`](../../docs/coord/BUS-V3.md) → "Where a fold's
  answer came from".

Codex's repaired watch prompt already does the review sweep; the **directive raw-bus fallback is the
same discipline for the inbox side** — the installer-generated watcher (§2) and every adapter tick
(below) run the composite engine path (`briefing` first) and honor both markers before reporting.

**Claude Code / Cowork** — settings.json hooks.
```bash
./scripts/claude-code/install-claude-code.sh <team> <agent>
./scripts/claude-code/install-claude-code.sh --uninstall <team> <agent>
```
Writes three scripts to `~/.claude/fulcra-agent-hooks/` and merges their command paths into
`~/.claude/settings.json`: **SessionStart** → bounded `continuity resume` + inbox brief injected as
context; **PreCompact** and **SessionEnd** → backgrounded `continuity park`. It touches only its own
command paths; a coord2-era `fulcra-coord2-hooks` install is migrated to the new
dir in place. Pre-coord first-generation hooks are not managed by this installer
and should be removed separately. Cowork uses the same core
and the same settings.json, so the same installer covers it.

The SessionStart hook also consumes exact-identity queued wake nudges from
`${COORD_WAKE_DIR:-${XDG_STATE_HOME:-~/.local/state}/coord-engine/wakes}` before the briefing.
These files are written atomically and keyed by the router idempotency key; duplicate delivery
self-overwrites, consumption happens once, and no event body or encoded command enters model context.
Malformed files remain in place and surface a degraded marker instead of being silently discarded.

**Codex** — `hooks.json` merge + app-thread automation.
```bash
python3 scripts/codex/install_codex_watch.py <team> <agent> [--codex-dir DIR] [--thread-id ID] [--interval-minutes N] [--uninstall] [--dry-run]
```
Merges SessionStart (matcher `startup|resume|clear|compact`) + PreCompact entries into
`~/.codex/hooks.json` — same entry shape as Claude Code — and seeds a coord-first app-thread automation
under `~/.codex/automations/coord-watch-<agent>/` whose prompt embeds contract rules 1–3 and ticks the
inbox. The default safety-net cadence is 30 minutes (override with `--interval-minutes`), replacing the
old 5-minute model-backed poll. For event-driven wake, register its exact thread id with the wake
router (host-local `codex-exec-resume` adapter in `_coord/router/config.json`). The adapter uses the stable
`codex exec resume <SESSION_ID>` interface, never passes
`--dangerously-bypass-approvals-and-sandbox`, and never places raw bus event text in the prompt; the
resumed agent fetches authoritative briefing state. Deployment precondition: on the first real host, verify the SessionStart hook actually fires
before relying on hook-based automation seeding — pass `--thread-id` for the deterministic path if you
already know the watch thread.

Codex SessionStart consumes the same queued-wake directory and exact-identity format as Claude Code.
The host executor invokes `coord-engine wake queue-file <team> --agent <id> --key <idempotency-key>`;
the hook invokes `coord-engine wake consume <team> --agent <id>`. Both commands are model-free.

**OpenClaw** — managed prose block.
```bash
python3 scripts/openclaw/install_openclaw.py <team> <agent> [--workspace DIR] [--uninstall] [--dry-run]
```
Merges a `fulcra-agent`-fenced block (`<!-- fulcra-agent:begin … -->` / `<!-- fulcra-agent:end -->`)
into the workspace's `HEARTBEAT.md` and `BOOT.md`, embedding contract rules 1–2 for OpenClaw to read at
boot and on each heartbeat tick. The managed blocks are intentionally compact because OpenClaw reads
them repeatedly; detailed doctrine stays in this skill. Rule 3 (park on shutdown) is **not automated** — the prose-block layer
has no shutdown hook to fire it, so it must be followed as prose. This is the **prose-block layer
only** — no hooks-dir machinery. It
validates marker balance before any write and **refuses (exit 1) on unbalanced or crossed markers**
rather than risk destroying user content between an orphan marker and an appended one.

**Hermes (Daytona sandbox)** — provisioned out of band: the `fhd` provisioner that installs the Claude
Code adapter inside the sandbox lives in the standalone `hermes-daytona` repo (extracted from this
monorepo) and is tracked there, not here.

**Claude web / Cowork-cloud tier (best-effort):** cloud sessions have no persistent
filesystem or scheduler on your machine. Follow the lifecycle contract as prose
(fulcra-agent-continuity §contract), and use the platform's scheduled routines to
open a periodic duty-cycle session that runs steps 1–2 of the contract. There is
no durable background pickup on this tier; anything that must not wait for a
routine belongs with an agent on a host tier.

The router's `routine-align` adapter does not wake or create a cloud session. It writes one
idempotency-keyed standard delivery record under `team/<team>/_coord/router/delivered/`, extended with
`mode: self-armed-routine`, `eligible_at`, and `no_session_created: true`. For this lane, “delivered”
means alignment-recorded: queued work has been aligned to the agent's already-existing Routine, and
the Routine's normal
SessionStart/briefing reads the authoritative bus. Treating this marker as an exact-session wake is a
contract violation.

## 4. Resume on wake — structured, not a prose re-read
When a cron/heartbeat wakes an agent to do team work, the wake payload should **resume continuity first**
(this is the structured version of `fulcra-agent-teams`' "read progress.md before acting" rule):
```bash
coord-engine continuity resume <team> <agent>
coord-engine queue <team> --agent <agent>      # the wake surface: read + process + commit
```
Then process the team inbox and, before concluding, snapshot again
(`coord-engine continuity snapshot …`) and let the next reconcile heal the views.

## 5. Recommended loop for a team
1. **Heartbeat**: `install-heartbeat.sh <team>` — reconcile every ~20m (consent first).
2. **On wake**: `continuity resume` → `queue` (read/process/commit) → do work (`task …`, inbox) →
   `continuity snapshot` → `reconcile`.
3. **Gate merges** with `fulcra-agent-review` (`review status`), and keep roles fresh with
   `fulcra-agent-roles` (`roles status`), escalating vacancies.

That's the full coord stack running unattended on top of a `fulcra-agent-teams` space.

See the bundled [`scripts/install-heartbeat.sh`](scripts/install-heartbeat.sh).
