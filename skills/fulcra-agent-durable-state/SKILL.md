---
name: fulcra-agent-durable-state
description: "Keep an agent's tooling and working state alive across ephemeral machines by treating the Fulcra File Store as the durable home and local disk as cache: restore on wake, push on change, and never stash a secret in a shared team path."
homepage: "https://github.com/ashfulcra/fulcra-tools"
license: "MIT"
user-invocable: true
metadata: { "openclaw": { "emoji": "🗃️" } }
---

# Fulcra Agent Durable State

**Ephemeral compute + durable Fulcra state = agents that survive their machines.**

Cloud agent containers get reclaimed and rolled back. Desktop hosts sleep, reboot,
and disappear. Any agent whose tooling lives only on local disk is one filesystem
event away from amnesia — and worse than losing a script is *silently regaining an
old one*: a rollback can revert a patched loop to the unpatched version, or restore
a rotated credential's stale predecessor, and nothing errors until it misbehaves.

The Fulcra File Store is the piece of the platform that doesn't share the machine's
fate. It's the same substrate the coordination bus runs on (versioned,
last-writer-wins, survives every container the fleet has lost so far), and
`fulcra-api` auth persists independently of scratch disk. So the pattern is:

> **Local disk is a cache. The store is the truth.**

## The stash convention

Each agent keeps its durable bundle under a per-agent path in the team namespace:

```
team/<team>/_coord/agents/<agent>/stash/
    linear-sync.sh        # scripts, loops, config templates
    listener-loop.sh
    restore-tooling.sh    # the self-heal entrypoint (see below)
```

Three behaviors, mirroring the continuity lifecycle contract:

1. **On wake** (fresh session, cron fire, post-rollback): if a tool you expect is
   missing from local disk, restore before improvising:
   ```bash
   fulcra-api file download team/<team>/_coord/agents/<agent>/stash/<file> <local-path>
   chmod +x <local-path>
   ```
2. **On change** (you edit a script, fix a bug in a loop): push the canonical copy
   back immediately — an unstashed fix is a fix a rollback will undo:
   ```bash
   fulcra-api file upload <local-path> team/<team>/_coord/agents/<agent>/stash/<file>
   ```
3. **Self-heal first.** Keep a `restore-tooling.sh` in the stash that downloads the
   rest of the bundle, and make it the first line of any scheduled job's
   missing-file branch. A scheduled task's prompt should say "restore from the
   stash", not "rebuild from memory" — session memory compacts; the store doesn't.

## The secrets rule (this is most of the lesson)

**Never put a secret in the stash.** `team/<team>/**` is readable by every agent on
the bus — that is the point of a bus. A token in a shared path is a token every
agent (and every prompt-injection that lands on any agent) can read. Fail closed:

- Credentials go in the harness's environment configuration (injected env vars),
  the OS keychain (as Collect does), or an operator-held channel — never
  `*.env` files in the stash, no matter how convenient the restore would be.
- Treat filenames as a first filter: if it's called `.env`, `*.key`, `*token*`,
  or holds a known credential prefix (`lin_oauth_…`, `sk-…`, PEM headers), it
  does not get uploaded. When in doubt, it's a secret.
- Config *templates* with the secret redacted are fine — the restore path then
  needs only the secret from env config, not the whole file from memory.

The asymmetry is deliberate: losing a script costs one download; leaking a
credential costs a rotation and an incident.

## Case study (why this skill exists)

One fleet coordinator running in a rollback-prone cloud container lost its
scratch tooling to filesystem reverts repeatedly — three times in a single day at
the worst. Each loss cost a from-memory rebuild, and two reverts were *silent
downgrades*: a listener loop whose disabled escalation step came back armed, and
a credentials file whose rotated-out key came back looking healthy (the first
symptom would have been a mystery `401` an hour later). Moving the bundle into a
File Store stash turned recovery into one download, made the store copy the
arbiter of "which version is current", and kept the rotated token out of shared
paths entirely — it rides in environment configuration instead.

## Relationship to the other skills

- **fulcra-agent-continuity** checkpoints *narrative* state (objective, decisions,
  next actions). This skill durably stores *operational* state (the scripts and
  config that let you act at all). A waking agent wants both: `continuity resume`
  for what it was doing, a stash restore for what it works with.
- **fulcra-agent-automation** installs the scheduled jobs; this skill is where
  those jobs' self-heal branches point.
- Deterministic bookkeeping for this pattern (a `stash push/pull` verb with a
  manifest and a fail-closed secrets guard) is planned for `coord-engine`; until
  it lands, the plain `fulcra-api file` commands above are the whole mechanism.
