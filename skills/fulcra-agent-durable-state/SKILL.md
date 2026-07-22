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

## Where to start — the re-entrancy probes

Before touching the stash, probe how far this session already got. Enter at the **first probe
that fails** (per the repo's skill-quality pattern, `docs/skill-quality-pattern.md`); every step
is safely re-runnable (uploads/downloads are whole-file overwrites, last-writer-wins). Auth
probes on `fulcra-api` (the engine inherits its credentials); the stash probes run on the
engine's `stash` verb:

| Probe (run in order) | Command | Passes when | If it fails, enter at |
|---|---|---|---|
| Auth usable? | `fulcra-api auth print-access-token >/dev/null && echo AUTH-OK` | prints `AUTH-OK` (exit 0) | `fulcra-api auth login` — browser sign-in; a headless agent that can't complete it should surface to its operator, not improvise |
| Stash exists? | `coord-engine stash list <team> --agent <agent>` | lists at least one file (not `empty`) | **First adoption** — nothing durable yet: push your bundle now (see *On change* below) |
| Local cache complete? | `test -x <local-path>` for each tool the stash lists | every tool you depend on exists locally and is executable | **Restore** — `coord-engine stash pull <team> --agent <agent> --dest <dir>` (see *On wake* below) |

All probes pass → your tooling is durable and current; work normally and push on change. A
freshly rolled-back container typically fails the third probe and enters at Restore.

## The stash convention

Each agent keeps its durable bundle under a per-agent path in the team namespace:

```
team/<team>/_coord/agents/<agent>/stash/
    manifest.json         # written by `stash push`: per-file sha256 + size + exec bit
    linear-sync.sh        # scripts, loops, config templates
    listener-loop.sh
    restore-tooling.sh    # the self-heal entrypoint (see below)
```

Three behaviors, mirroring the continuity lifecycle contract:

1. **On wake** (fresh session, cron fire, post-rollback): if a tool you expect is
   missing from local disk, restore before improvising:
   ```bash
   coord-engine stash pull <team> --agent <agent> --dest <dir>
   ```
   Pull re-applies each file's executable bit from the manifest and verifies its
   sha256 — **checksum drift exits loud (rc 1)** instead of handing you a
   silently-diverged restore. (No engine on the host yet? The plain
   `fulcra-api file download` path still works; the stash is ordinary files.)
2. **On change** (you edit a script, fix a bug in a loop): push the canonical copy
   back immediately — an unstashed fix is a fix a rollback will undo:
   ```bash
   coord-engine stash push <team> <local-path> --agent <agent>
   ```
   One step uploads the file and refreshes `manifest.json`; the push runs the
   fail-closed secrets guard below.
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
  does not get uploaded. When in doubt, it's a secret. `stash push` enforces
  exactly this, fail-closed: secret-shaped names and credential-shaped content
  are refused with the tripped rule named; `--unsafe-allow-secrets` exists for
  a false positive only, never for a real credential.
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
- The deterministic bookkeeping for this pattern is the engine's
  `stash push/pull/list` verb (manifest + sha256 checksums + the fail-closed
  secrets guard); the plain `fulcra-api file` commands remain the no-engine
  fallback — the stash is ordinary files either way.
