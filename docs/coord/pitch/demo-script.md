# 5-minute live demo script — coord on team/fulcra

*Run on real production data. No slides. Every command is copy-pasteable from this file.*
*Prep (once, before the meeting): `export FULCRA_COORD_AGENT=coord-maintainer`; confirm `coord-engine doctor fulcra` is green; have this file open.*

## 0:00 — the space is just teams (30s)

> "This is a stock fulcra-agent-teams space — OKF markdown in the file store. Everything you're
> about to see layers on top; nothing modified teams."

```bash
fulcra-api file list team/fulcra/ | head
```

## 0:30 — deterministic task views (60s)

> "The task index is engine-owned: any agent, any host, same answer."

```bash
coord-engine briefing fulcra          # one-read morning brief
coord-engine board fulcra             # status-grouped board
coord-engine needs-me fulcra --agent coord-maintainer
```

Point out: typed statuses, done-requires-evidence, the state machine (`proposed→active→done`)
enforced in code — an illegal transition is an error, not a style violation.

## 1:30 — presence + roles: who's alive, who's responsible (60s)

```bash
coord-engine agents fulcra            # live/idle/stale fold + what each agent is on
coord-engine roles status fulcra coord-maintainer --json
```

> "Roles are claimable leases. Exclusive policy: two DIFFERENT ids contending shows CONTESTED.
> And the same-id blind spot — two sessions accidentally sharing an identity — is caught by a
> session nonce in the lease; the second session gets a loud warning on refresh."

```bash
coord-engine roles claim fulcra coord-maintainer    # note the shard echo; re-run = refresh
```

## 2:30 — the operator loop: nothing waits silently (90s)

> "The one that paid for itself on day one. Agents that hit a wall file a structured ask; the
> orchestrator pulls this fold on every heartbeat; the operator's answer flows back as ONE atomic
> write — unblocked, handed to the owner, marker stripped."

```bash
coord-engine asks fulcra              # oldest-first, age in hours
```

> "First day this ran, it surfaced an ask that had been buried for 26 days. Ash answered in one
> sentence; the task was back in the owner's inbox seconds later."

(If an ask is present, answer a staged one live: `coord-engine answer fulcra <slug> --with "..."`.)

## 4:00 — reviews + fleet health (45s)

```bash
coord-engine review status fulcra <live-review-slug> --json   # APPROVED/CHANGES/PENDING fold
coord-engine health fulcra            # which hosts heal the team; who went dark
```

## 4:45 — close (15s)

> "Twelve skills, one stdlib-only engine, 800+ tests, dual AI review on every PR. Wave 1 is six
> purely-additive skills. The engine's natural first home is a small Fulcra-owned repo — folding
> into fulcra-api stays on the table as the long-term convergence, sized with your team. Evidence
> pack is one page in DESIGN.md."

## Fallbacks

- No live ask at demo time → stage one 10 min before: `coord-engine task start fulcra "Demo: pick deploy window" --status active && coord-engine task block fulcra demo-pick-deploy-window --on-user "window A (tonight) or B (weekend)?"`
- Network hiccup → screenshots of each command output, captured at prep time, in `docs/pitch/demo-fallback/`.
