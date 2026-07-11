# Get on the bus — coord from zero

The shortest path from "I have an agent" to "it coordinates durable work with other
agents over a shared bus." Every step here was verified live by a remote agent joining
cold; the remote-sandbox section exists because that agent hit every one of those walls.
Why the design looks like this: [`COORDINATION-PROTOCOL.md`](../../COORDINATION-PROTOCOL.md).
Conventions once you're on: [`AGENTS.md`](../../AGENTS.md).

## 1. Prerequisites

- A [Fulcra](https://fulcradynamics.com) account (created on first login) — the bus is
  your Fulcra File Store; there is no broker or server to run.
- [`uv`](https://docs.astral.sh/uv/).
- Optional: the official [`fulcra-agent-teams`](https://github.com/fulcradynamics/agent-skills)
  skill is the base prose convention (members, inboxes) that the `fulcra-agent-*` skills
  here enhance. The engine works without it — you can start from a bare team name.

## 2. Install

```bash
uv tool install fulcra-api        # the `fulcra` CLI: auth + the file transport
uv tool install "git+https://github.com/ashfulcra/fulcra-tools@coord-engine-v1.5.0#subdirectory=packages/coord-engine"
```

(From a checkout: `uv tool install ./packages/coord-engine`. `coord-engine` is not on
PyPI yet, so `uvx` / `uv tool run coord-engine` will NOT resolve it — use the installed
binary.) Install the skills into your agent with
[`scripts/coord/coord-setup.sh`](../../scripts/coord/coord-setup.sh).

## 3. Authenticate

Interactive (a browser opens):

```bash
fulcra auth login
```

Headless (no browser on this host): run `fulcra auth login --get-auth-url`, open the
printed URL on any device, then finish with
`fulcra auth login --device-code <DEVICE_CODE>`.

### Remote / sandboxed environments (Claude Code cloud, CI, proxied containers)

Three walls, in the order you'll hit them:

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
   Client constants live in `fulcra_api/core.py`. Token *refresh* has the same
   limitation; expect to re-run the flow when the access token expires.
3. **Ephemeral hosts.** Credentials and tool installs die with the container. Put the
   two installs and the egress requirement in the environment's setup script; expect to
   re-auth per session until a secrets store exists.

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

## 6. Stay on the bus

- **`coord-engine listen <team> --agent <you>`** is the engine-owned watcher for new
  directives, responses, and verdicts — never hand-roll one. `--once` prints nothing
  when quiet and always exits 0, so it composes with any scheduler.
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

## 7. Where next

- [`AGENTS.md`](../../AGENTS.md) — the working conventions: review handshake, delivery
  rule, backlog, ATC routing.
- [`skills/`](../../skills) — the twelve `fulcra-agent-*` skills, each with re-entrancy
  probes telling a waking agent exactly where to enter.
- [`docs/coord/pitch/`](pitch) — the one-pager and demo script, if you're evaluating
  whether to adopt this at all.
