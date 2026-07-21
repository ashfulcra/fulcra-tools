---
name: fulcra-agent-directives-cli
description: "coord-engine directive verbs + the ack shard shape."
---

# Fulcra Agent Directives — CLI reference

All verbs create/read ordinary Task docs (`team/<team>/task/<slug>.md`) — a directive is a task with an
`assignee`. Run `coord-engine reconcile <team>` (or let the heartbeat) to refresh the inbox aggregate.

```bash
coord-engine tell      <team> <assignee> "<title>" [-p P1] [-s "…"] [-n "…"] [--from <me>]
coord-engine broadcast <team> "<title>" [flags]                  # assignee '*'
coord-engine remind    <team> <assignee> <when> "<title>"        # when: ISO | 5d | 36h | 10m
coord-engine later     <team> "<title>"                          # @backlog
coord-engine intent    <team> "<text>" --for <principal> [--by <when>] [--from <me>] [-p P1]
coord-engine inbox     <team> --agent <X> [--json] [--all]       # --all = acknowledged/closed/future/@backlog history
coord-engine needs-me  <team> --agent <X> [--json] [--all]       # same history opt-in
coord-engine briefing  <team> --agent <X> [--json] [--all]       # same queue semantics in the session bundle
coord-engine inbox     <team> --agent <X> --ack <slug>
coord-engine respond   <team> <slug> --outcome "…" [-e "…"] [--agent <X>]
coord-engine threads   <team> --for <principal> [--silence-days N] [--intent-grace-hours N] [--json]
```

Ack shard (`team/<team>/_coord/acks/<slug>/<agent-key>.md`):
```yaml
---
type: Ack
agent: claude-code:host:repo
timestamp: 2026-07-02T12:00:00Z
---
```
The filename key is collision-safe (`slug+sha1[:6]`); reconcile trusts the frontmatter `agent:` only when
it round-trips to the filename. Response shards live at `_coord/responses/<slug>/<stamp>.md`.

Notes: if an agent's raw id changes, its `agent_key` changes and old acks stop applying (it gets
re-notified under the new identity — intentional). `respond` performs no assignee authorization — anyone
on the team can close a directive (the File Store write ACL is the trust boundary). Reconcile GC only
deletes ack shards that are datable AND older than 24h AND whose task is absent from a non-empty listing.

## `intent` — capture a spoken commitment

```bash
coord-engine intent <team> "<text>" --for <principal> [--by <when>] [--from <me>] [-p P1]
```
Sugar over the directive path: writes an `intent:<principal>` item (a Task with
`assignee` + `intent_by` frontmatter) through the same hash-slug delivery + read-back
as `tell`. Use it the SAME turn a principal states a commitment ("later today", "I'll
enumerate that list") — an uncaptured commitment is the drop nobody can see, and
`threads` only surfaces what was recorded.

- `--for <principal>` — who owes the commitment (e.g. `ash`).
- `--by <when>` — declared window (ISO or `5d`/`36h`/`10m`); absent = undeclared, and
  the `threads` fold falls back to capture-time + `--intent-grace-hours`.
- **Identity is text + assignee only — `--by` is EXCLUDED from the slug.** So an
  identical restatement dedupes (**rc 0** `intent already captured`), while a
  restatement with a DIFFERENT `--by` is a verified in-place **window update** on the
  same doc (**rc 0** `intent window updated`, read-back-checked; unverifiable → **rc 1**,
  retry — never a stale deadline, never a forked item). A relative `--by` re-resolves
  from now on each restatement.

## `threads` — the dropped-work fold

```bash
coord-engine threads <team> --for <principal> [--silence-days N] [--intent-grace-hours N] [--json]
```
A read-only fold of work-in-progress a principal has let drop. Three
mutually-exclusive modes (first match wins):

- **started-then-silent** — an item they own/last-touched whose activity is older
  than `--silence-days` (default 3).
- **blocked-on-`<principal>`** — progress waits on them (`assignee: <principal>`, a
  `blocked-on:<principal>` tag, or a `needs:human` block naming them); surfaced
  immediately, no aging.
- **intent-never-started** — an `intent:<principal>` item past its window (`intent_by`
  if declared, else capture + `--intent-grace-hours`, default 48) with no follow-up
  (a status advance, a response shard, or a `followed-up-by:` tag each discharge it).

A **terminal item (`done`/`abandoned`) is never a dropped thread** in any mode — the
fold refuses it and reads the authoritative status from the task doc, not the summaries
index (a same-minute close can leave the index stale-`proposed`).

**Windows / env:** `--silence-days` / env `COORD_THREADS_SILENCE_DAYS`;
`--intent-grace-hours` / env `COORD_THREADS_INTENT_GRACE_HOURS`. The fold is wall-clock
bounded by `COORD_THREADS_FOLD_BUDGET` (default 30s; a bad value falls back to the
default, never disables the bound).

**Degraded:** a **`threads-degraded` row** (a JSON object under `--json`; a stderr notice
in text mode) means the fold saw only PART of the store — sweep or wait, **never trust it
as complete**.
