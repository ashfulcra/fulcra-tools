# Agent harness docs live on the team's bus, not in this repo

Each agent instance keeps a **harness self-description** — its wake sources, how
it survives container resets, the operating rules that bind it, its cold-start
reading list. Those documents are specific to one team's deployment: they name
that team's machines, sessions, schedules and operator grants.

**This is a public, general-purpose toolkit, so they do not live here.** They
live in the team's own file store, next to the rest of that agent's durable
state:

    team/<team>/_coord/agents/<name>/harness.md

For the fulcra team the four docs that used to sit in this directory are at
`team/fulcra/_coord/agents/<name>/harness.md`, moved 2026-08-08 and verified
readable there before the repo copies were removed.

## The convention (what a harness doc contains)

1. **Cold start** — the ordered reading list a successor follows, ending at the
   store's own authorities (`adopt-latest.sh`, `records.json`, `BOOTSTRAP.md`),
   which outrank every document.
2. **What this agent is** — one paragraph; which harness pattern it implements
   (see [`skills/`](../../../skills) for the assembled patterns).
3. **Wake sources**, most durable first, and what survives a handoff vs a
   container reset.
4. **Container-reset survival** — what is cache, what is durable, and the one
   manual step (if any) that needs a human.
5. **Operating rules that bind the role** — including the standing instruction
   to capture harness-specific behaviour in
   [`HARNESS-MAP.md`](../HARNESS-MAP.md) in the same pass that discovers it.

Split by **change rate**: what the *role* is belongs in the role's charter
(`team/<team>/roles/<name>/`); how *this instance* runs belongs in the harness
doc and dies with the instance. A document that mixes both gets edited five
times in two days — measured, not guessed.

6. **Environment manifest** — a sibling `environment.json`
   (`coord.agent-environment/v1`): the plugins, CLI tools and credentials a
   successor must restore, each with its **install commands, a verify command,
   and what survives** a container restart vs a reclaim vs a session handoff.
   Installed plugins are part of the role definition (operator ruling,
   2026-08-08): a park that does not carry the environment is a park that loses
   capabilities silently — that is not hypothetical, it happened across a real
   handoff and was noticed two days later. Restore is part of the join, and the
   verify line is the artifact ("installed" is a proxy).

Keep the doc current in the same pass that changes the harness. An agent doc
that lags its agent is a cold-start list that walks a successor into a wall.
