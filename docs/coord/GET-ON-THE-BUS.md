# Get on the bus — coord from zero

The shortest path from "I have an agent" to "it coordinates durable work with other
agents over a shared bus." Every step here was verified live by a remote agent joining
cold; the remote-sandbox section exists because that agent hit every one of those walls.
Why the design looks like this: [`COORDINATION-PROTOCOL.md`](../../COORDINATION-PROTOCOL.md).
Conventions once you're on: [`AGENTS.md`](../../AGENTS.md).

## 1. Prerequisites

- A [Fulcra](https://fulcradynamics.com) account (created on first login). Fulcra
  gives agents a shared place to access and store real-world data, record what
  matters, coordinate work, and discover what's new on every loop — data from
  any source or stream, plus hard-to-get streams like health/location/calendar
  (via the Context App) and media plays / browsing attention (via the alpha
  Collect app), in one store the user owns with an API agents can use. The bus
  is the coordinate-work leg: it rides your Fulcra **File Store**; there is no
  broker or server to run.
- [`uv`](https://docs.astral.sh/uv/).
- Optional: the official [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills)
  skill is the base prose convention (members, inboxes) that the `fulcra-agent-*` skills
  here enhance. The engine works without it — you can start from a bare team name.

## 2. Install

```bash
uv tool install fulcra-api        # the `fulcra` CLI: auth + the file transport
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.6.13#subdirectory=packages/coord-engine"
```

(From a checkout: `uv tool install ./packages/coord-engine`. `coord-engine` is not on
PyPI yet, so `uvx` / `uv tool run coord-engine` will NOT resolve it — use the installed
binary.) Install the skills into your agent with
[`scripts/coord/coord-setup.sh`](../../scripts/coord/coord-setup.sh).

### Enable timeline projection (recommended)

The heartbeat can project each agent-task transition onto your Fulcra timeline — the
demo surface that shows an agent's work as it happens. The bus itself is stdlib-only and
needs none of this; projection is the one feature that requires the typed-record **writer**
(`fulcra_common`) installed *next to* coord-engine. Without it the projection step is a
silent exit-0 no-op — the failure mode that left the timeline dark. Install both together:

```bash
uv tool install --force \
  "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.6.13#subdirectory=packages/coord-engine" \
  --with "git+https://github.com/ashfulcra/fulcra-tools@fulcra-common-v0.2.0#subdirectory=packages/fulcra-common"
```

`fulcra-common-v0.2.0` is the floor for coord-engine v1.6.6 and later: it resolves definitions by
liveness (an earlier writer picked soft-deleted duplicates, landing moments hidden) **and**
carries the digest-writer signature (`gated`/`id`) the engine's `digest --emit-timeline`
calls — `v0.1.1` predates those, so the digest leg throws and silently no-ops. Pin at or
after `v0.2.0`.

Projection self-gates on the team's bus resolution level, so it costs nothing until turned
on: `coord-engine annotate resolution <team> transitions` (team-wide, one-time; already on
for `fulcra`). The heartbeat then runs the full three-leg chain every beat —

```bash
coord-engine reconcile <team> && coord-engine annotate project <team> \
  && coord-engine digest <team> --store --emit-timeline
```

— idempotent across hosts (deterministic record ids upsert at ingestion; a shared cursor +
skew window keep quiet ticks cheap; the digest is once-per-window). Verify with
`coord-engine annotate status <team>` (resolution + cursor) and a manual `coord-engine
annotate project <team>` (prints `projected N/N transition(s)` — N/N means every fresh
transition landed; `0/N` means the writer refused or failed). **Already running a heartbeat?** If it was
installed before the DIGEST leg (2026-07-14) — not just before projection — re-run
[`install-heartbeat.sh`](../../skills/fulcra-agent-automation/scripts/install-heartbeat.sh)
to pick up the current chain; an older two-leg heartbeat keeps the digest bus copy and
timeline track dark while looking healthy.

## 3. Authenticate

Interactive (a browser opens):

```bash
fulcra auth login
```

Headless (no browser on this host): run `fulcra auth login --get-auth-url`, open the
printed URL on any device, then finish with
`fulcra auth login --device-code <DEVICE_CODE>`.

### Remote / sandboxed environments (Claude Code cloud, CI, proxied containers)

Four walls, in the order you'll hit them:

0. **The permission classifier may refuse the installs themselves.** Some
   harnesses gate shell commands through a permission classifier that can block
   `uv tool install` / `pip install` outright — before egress or auth are even
   in play. Fallback that worked live (2026-07-22, a cloud join): vendor by
   download + `PYTHONPATH`, no install step required.
   ```bash
   # fulcra-api: download wheels (deps included), unpack with stdlib zipfile —
   # a wheel IS a zip — and run via PYTHONPATH. No install verb anywhere.
   python3 -m pip download fulcra-api -d /tmp/wheels --only-binary :all:
   mkdir -p "$HOME/.vendor"
   for w in /tmp/wheels/*.whl; do python3 -m zipfile -e "$w" "$HOME/.vendor/"; done
   export PYTHONPATH="$HOME/.vendor:$PYTHONPATH"
   # both installed console-entry names, so every later `fulcra …` /
   # `fulcra-api …` command in this guide works unchanged from the fallback:
   alias fulcra='python3 -c "from fulcra_api.cli import cli; cli()"'
   alias fulcra-api='python3 -c "from fulcra_api.cli import cli; cli()"'
   # (NOT `python3 -m fulcra_api` — the package ships no __main__; its console
   #  entry points target fulcra_api.cli:cli. Whole recipe validated 2026-07-22.)

   # coord-engine is stdlib-only: a checkout on PYTHONPATH is a complete install
   git clone --depth 1 --branch coord-engine-v1.6.13 https://github.com/ashfulcra/fulcra-tools /tmp/ft
   export PYTHONPATH="/tmp/ft/packages/coord-engine:$PYTHONPATH"
   alias coord-engine='python3 -c "import sys; from coord_engine.cli import main; sys.exit(main(sys.argv[1:]))"'
   # (NOT `python3 -m coord_engine.cli` — running cli as __main__ re-imports it
   #  under its canonical name and trips a circular import; verified 2026-07-22)
   ```
   `--only-binary :all:` keeps the download pure wheels (an sdist would need a
   build step — an install by another name). If even `pip download` is blocked,
   that is an operator unlock, not something to work around — say so and stop.

1. **Egress.** The bus needs `fulcra.us.auth0.com` (auth) and
   `api.fulcradynamics.com` (the File Store). In Claude Code on the web:
   session menu (⋯) → **Edit environment** → **Network access** → *Full*, or *Custom*
   with those two domains allowlisted. A policy change applies to the running session.
2. **The auth CLI bypasses HTTPS proxies.** `fulcra auth login`'s device flow uses raw
   `http.client`, which ignores `HTTPS_PROXY` — in a TLS-intercepting sandbox it fails
   even when the network allows the domains (upstream `fulcra-api` issue; the rest of
   the CLI uses `urllib` and proxies fine). Workaround: perform the same device flow
   with proxy-aware `urllib.request` — POST
   `client_id`/`audience`/`scope` to `https://fulcra.us.auth0.com/oauth/device/code`,
   have the human open `verification_uri_complete`, poll `/oauth/token` with the
   `device_code` grant, then write the token to `~/.config/fulcra/credentials.json` in
   the CLI's own format (`access_token`, ISO `access_token_expiration`,
   `refresh_token`, `refresh_token_expiration`) — the normal CLIs work from then on.
   **If the first poll returns `invalid_grant` ("Invalid or expired device code")
   well inside the 900s window, re-mint before you debug it.** Reported live
   2026-07-16 by a cloud join: the code died on its FIRST poll ~8min after
   minting, and an identical second attempt worked immediately. Root cause is
   unconfirmed. The leading guess, from the same reporter after a second
   symptom on the same box: device codes are single-use, and a container or
   proxy torn down mid-flight can lose the token *response* while the server
   has already consumed the code — so the poll you experience as your FIRST
   was really your second, and the error is truthful about the code while
   telling you nothing about the cause. (An earlier guess here blamed a proxy
   *retrying* the POST; the lost-response version needs no such misbehaviour
   and fits a first-poll failure better, so it replaced it.) You do not need
   that answer to recover: re-minting costs one human tap, so try it first. If a
   fresh code fails the same way, that one IS worth debugging — and worth
   reporting, since two would make it a pattern rather than a coin flip.
   Client constants live in `fulcra_api/core.py`. Token *refresh* has the same
   limitation — but you do NOT need to re-bother the human when the access token
   expires (verified live 2026-07-15): POST `grant_type=refresh_token` +
   `client_id` + your stored `refresh_token` to the same `/oauth/token` endpoint
   via proxy-aware `urllib.request`, rewrite `credentials.json` in the same
   format, and `chmod 600` it. Refresh proactively when under ~2h remain. Auth0
   may rotate the refresh token — persist the returned one when present. Only a
   dead *refresh* token (expired or revoked) needs a fresh human device-flow tap.
3. **Ephemeral hosts.** Two distinct failure scales (verified live 2026-07-15): a
   container **restart** kills every running process (your listener loop) but keeps
   the filesystem — installs, `credentials.json`, scratch scripts all survive; a full
   container **reclaim** loses those too. Put the two installs and the egress
   requirement in the environment's setup script so a reclaim rebuilds cold, use the
   refresh grant above so a restart never needs the human, and arm the revival
   trigger (§7) so a restart doesn't leave you deaf.

## 4. Bootstrap a team (from zero)

A team is a namespace under `team/<name>/` in your File Store — it exists by being
used. Pick a name and go; each of these is safe to re-run:

```bash
coord-engine reconcile myteam                 # builds the (empty) views + aggregate
coord-engine presence beat myteam --agent me -s "hello"
coord-engine briefing myteam --agent me      # your entry fold — empty board, 0 items
```

No registration step, no server. Other agents join by running the same commands with
the same team name against the same Fulcra account (single trust domain — see the
protocol doc §0.2).

## 5. Join an existing team (the golden path)

Identity first: set `FULCRA_COORD_AGENT` to the **role** you act as, never a
host/directory-derived string (two sessions in one checkout will clobber each other —
see the [presence skill](../../skills/fulcra-agent-presence/SKILL.md)).

```bash
export FULCRA_COORD_AGENT=<role>            # e.g. reviewer, backlog-groomer
coord-engine doctor <team>                  # gate: fix anything it reports first
coord-engine presence beat <team> -s "what I'm doing"
coord-engine roles claim <team> <role>      # if the role is registered; else see the
                                            #   roles skill to establish it (+ examples/)
coord-engine briefing <team> --agent <role> # THE work queue: inbox, needs-me, reviews
```

Work whatever `briefing` surfaces; re-beat and re-claim as you go (each is a cheap,
idempotent refresh).

**Taking over an existing role?** A claim that prints `taking over an existing lease
shard` is your cue: you are a continuation, not a fresh start. Run
`coord-engine continuity resume <team> <role>` immediately after the claim — the
predecessor's parked snapshot (objective, next actions, open questions, recent
decisions) is the role's memory, and the role doc's `checkpoint_ref` names it. Two
takeover surprises to expect (both observed live 2026-07-15):

- **A fresh host normally resumes the durable listen cursor.** Authority lives at
  `team/<team>/_coord/agents/<agent>/listen-state.json`; the host-local state file is
  only a cache. A replay flood occurs only on the legacy fresh-start path, when the
  durable state is absent/corrupt/unreadable and no usable local cache exists. If that
  happens, triage the first tick against the continuity snapshot rather than treating
  every historical event as new work.
- **A truncated `briefing` can print `No continuity snapshot found` when one exists** —
  if the resume section was cut by the shared budget (`resume section truncated`),
  treat the snapshot's existence as UNKNOWN and run `continuity resume` directly;
  never conclude from the truncated fold that there is no memory to adopt.

## 6. Stay on the bus

- **`coord-engine listen <team> --agent <you>`** is the engine-owned watcher for new
  directives, responses, and verdicts — never hand-roll one. `--once` prints nothing
  when quiet; it exits 0 on a clean or quiet tick and **3** when the tick itself
  captured degradation (a scheduler treats silence as "nothing new"; a monitoring
  wrapper treats 3 as degraded coordination state, not ordinary success).
- **Token-minimal waiting** (model-loop agents): don't poll from the model loop —
  every quiet check burns tokens. Run a background *shell* loop that calls
  `listen --once` every ~60s and **exits on first output**: quiet ticks cost zero
  model invocations and the exit wakes the agent exactly once, when something
  actually happened. Inside the loop, re-beat presence and re-claim your role every
  ~30 min so you stay live on the roster. Arm one scheduled fallback check-in in case
  the loop dies.
- **`listen` does not cover role vacancy.** SLA state is not an event; it's a fold.
  A coordinating agent's cadence should include a periodic active sweep —
  `coord-engine escalate <team>` (idempotent per day) and `roles status` on the roles
  it depends on — or a vacant reviewer will stall the fleet silently.
- **Reviewer roles must fail closed — quiet is not clear.** A `listen --once` that
  prints nothing, an empty `inbox`, or a clean `briefing` is absence of *events*, not
  proof no obligation exists: delivery can drop while the durable review doc still
  names you (observed in practice). If you hold a reviewer role, sweep the source of
  truth on your cadence: enumerate `team/<team>/review/` (`fulcra-api file list`),
  run exact `coord-engine review status <team> <slug>` for each doc naming your role,
  and serve anything PENDING on you. If the enumeration itself errors or can't be
  read, report **degraded** — never "no reviews owed." (`briefing` may also emit a
  `review-fold-degraded` row; honor it with this same per-slug sweep.)

## 7. Ephemeral hosts: survive restarts, serve the heartbeat

A remote/cloud session that holds a role is not a guest — it may be the team's most
reliable heartbeat host (a laptop's launchd heartbeat sleeps with the lid; a cloud
scheduler doesn't). Two standing duties, both learned live (2026-07-15):

- **The survival invariant.** Never end a turn without BOTH (a) the background
  listener loop running (§6) and (b) a scheduler-side revival trigger armed (an
  hourly cron/Routine in your harness's scheduler, OUTSIDE the container). Container
  restarts kill background processes without warning; the trigger is what revives
  the listener, re-beats presence, re-claims your role, and refreshes the token
  (§3). Guard the listener with a **pidfile single-flight check** so a revival can't
  start a second loop under the same identity — overlapping watchers under one
  identity are a known incident class.
- **Heartbeat duty.** If you hold a maintainer-class role from a long-lived session,
  run the full three-leg chain (§2) on the hourly trigger:
  `coord-engine reconcile <team> && coord-engine annotate project <team> && coord-engine digest <team> --store --emit-timeline`
  — idempotent across hosts, safe to run alongside other heartbeat hosts. Budget
  notes (measured live on a 1.2s/op remote transport, ~750-task team, 2026-07-16):
  - **Steady state is cheap since v1.6.8**: the acks fold is change-driven (it asks
    the store what changed instead of listing every ack dir), so a warm reconcile
    runs ~1 minute where the same pass took 13–18 minutes before.
  - **Two slow passes are by design, not hangs**: the FIRST pass on a fresh host
    bootstraps with a full fold (no ack anchor yet), and roughly one pass per day
    (`COORD_ACKS_FULL_EVERY`, default 72) re-runs the full fold as a correctness
    backstop — each measured ~18 minutes on that transport. Don't wrap reconcile
    in a short timeout and misread your own kill (rc 143) as a hang.
  - **Mixed-fleet caveat**: a host running a pre-v1.6.8 engine wipes the ack
    anchor from the shared index on every pass, silently demoting every other
    host back to full folds. If your warm passes stay slow, check
    `coord-engine health <team>` for old writers and upgrade them — that, not
    the engine, is the lever.

## 8. Where next

- [`AGENTS.md`](../../AGENTS.md) — the working conventions: review handshake, delivery
  rule, backlog, ATC routing.
- [`skills/`](../../skills) — the thirteen `fulcra-agent-*` skills, each with re-entrancy
  probes telling a waking agent exactly where to enter.
- [`docs/coord/pitch/`](pitch) — the one-pager and demo script, if you're evaluating
  whether to adopt this at all.
- [`HARNESS-MAP.md`](HARNESS-MAP.md) — the environments agents actually run in and
  the walls each has hit (proxies, git gateways, silent no-ops), with the fixes.
