# wake-opencode — waking an OpenCode agent on the coord bus

Status: adapter script + tests shipped on branch `opencode-wake-adapter` (2026-07-25).
Live evidence: production instance on the shared Mac has been waking
Opencode-Kimi-Coder through exactly this mechanism since 2026-07-25T11:53Z
(report: `team/fulcra/_coord/agents/Opencode-Kimi-Coder/reports/2026-07-25-self-wake-manual-proof.md`).

## The harness facts (why this adapter looks like this)

- The OpenCode **desktop** app (Electron) embeds its opencode server with a
  per-launch Basic-auth password on an ephemeral port, persisted nowhere
  stable. There is no documented way to inject a prompt into a running desktop
  session, and when the app closes the agent is gone. The app registers an
  `oc://` URL scheme (undocumented). **A desktop-session agent cannot be woken.**
- The opencode server architecture supports **multiple servers on one data
  dir** (https://opencode.ai/docs/server/). So the supported wake topology is:
  run your OWN headless server and inject prompts into a pinned session on it.

## Mechanism

```
router invoker (W5.5 seam): opencode-wake.sh --agent <id> --key <k> --reason <fixed>
  └─ GET  /session/status        → if the pinned session is explicitly busy:
                                   coalesce (exit 0) — its standing orders read
                                   the full inbox every turn; nothing is lost
  └─ POST /session/<id>/prompt_async  → HTTP 204 → exit 0
       (loopback `opencode serve`, Basic auth, fixed content-free nudge body)
```

The nudge carries NO work content (plan §2 content rule): the prompt body is a
fixed template containing only the agent id, the idempotency key and the fixed
reason, telling the session to consume queued wakes, run its briefing and work
its inbox. The durable bus shards stay authoritative. At-least-once delivery is
safe: N fires converge to one bus check.

## The adapter contract

`skills/fulcra-agent-automation/scripts/wake/opencode-wake.sh`
`--agent <id> --key <idempotency-key> --reason <text>` — same argv shape and
charset gating as `macos-notify.sh`; unknown args exit 2; there is no flag
that accepts anything executable.

| rc | meaning |
|---|---|
| 0 | nudge posted (HTTP 204), or coalesced into an explicitly busy session |
| 1 | delivery failed (unreachable / non-204 after bounded retries) |
| 2 | usage / charset / config-credential fault — nothing was posted |
| 124 | exceeded local bound (only when timeout(1) exists) |
| 127 | curl or python3 unavailable |

`COORD_OPENCODE_WAKE_ATTEMPTS` (default 3, 5s apart) and
`COORD_OPENCODE_WAKE_TIMEOUT_SECONDS` (default 15) tune delivery.

## Credential handling (load-bearing)

The headless serve session has a port and a Basic-auth password. Rules,
enforced by the script and pinned by the test suite:

- Both are read **at invocation time** from a **mode-600** file (0600 or 0400;
  anything looser fails closed with rc 2).
- The password is **never** embedded in the script, **never in argv** (argv is
  world-readable via `ps` — a password there leaks to every process on the
  box), **never logged**, **never written to the store**. curl receives it via
  a config document on stdin (`curl -K -`). Passwords containing CR/LF, `"` or
  `\` are refused rather than quoted (same rule as the openclaw adapter).
- Prefer `PASSWORD_FILE` over inline `PASSWORD`: secrets stay where they were
  issued; a procedure that copies them anywhere is wrong even when convenient.
- `HOST` must be loopback; plaintext credentials never leave the box.
- Output is derived facts only: "posted to serve session, HTTP 204; key <k>".

### Config file

Path: `$OPENCODE_WAKE_CONFIG`, default `~/.config/opencode-wake/serve-session`
(mode 600). `KEY=VALUE` lines, strict whitelist, parsed without eval:

```
PORT=4196                       # required, loopback port of `opencode serve`
PASSWORD_FILE=/abs/path         # mode-600 file holding the Basic password
# PASSWORD=...                  # inline alternative (prefer PASSWORD_FILE)
SESSION_ID=ses_...              # required, pinned session to nudge
USERNAME=opencode               # optional
AGENT_NAME=bus-runner           # optional, agent addressed in the prompt body
HOST=127.0.0.1                  # optional, must stay loopback
```

## Provisioning (host executor)

Same explicit-provisioning rule as every host-local adapter: scripts are
located only under `COORD_WAKE_ADAPTER_DIR` as `opencode-wake.sh`. Unset ⇒
`unconfigured`, the wake stays VISIBLY QUEUED, nothing fires.

**Engine follow-up (NOT in this change):** adding `"opencode-wake"` to
`SCRIPT_ADAPTERS` in `wake_adapters.py` is a one-line engine PR to be sequenced
with the router repair (PR 481). Router `config.json` wiring is coord-boss's
decision plane and is deliberately untouched.

## Verification

### Harness-free (this is what a reviewer with no opencode install can check)

`packages/coord-engine/tests/test_wake_adapter_opencode_wake.py` — 25 tests,
all against a recording curl shim; no opencode server, no network:

```
cd packages/coord-engine && uv run --extra dev python -m pytest \
  tests/test_wake_adapter_opencode_wake.py -q     # 25 passed, 1 skipped (shellcheck)
```

Pinned: argv contract + rc table; charset refusal before curl runs; missing
curl/python3 → 127 fast; config/password-file mode enforcement; unknown config
keys; non-loopback refusal; password only on stdin, never argv/body/logs;
fixed body shape (agent+key+reason, nothing else); busy-coalesce without POST;
non-204/unreachable → rc 1; structural no-eval/no-`curl -u` grep.

Manual spot check (no opencode needed): point `OPENCODE_WAKE_CONFIG` at a
throwaway 600 config, run the script with a fake `--agent/--key/--reason`,
watch it fail closed (rc 1) against nothing listening — the failure path is
the contract.

### What CANNOT be verified without the harness

- That a real `opencode serve` accepts the POST and the pinned session wakes.
- The busy-coalesce semantics against a live session status endpoint.

Both are covered by live evidence instead: the production instance
(`~/.config/opencode-bus-runner/`, same mechanism) has been delivering wakes
since 2026-07-25T11:53Z — every autonomous runner turn on that host is an
existence proof, and this branch adds one direct adapter-level live fire
(reported on the bus).

## Install / disable (per host)

The reference server side is the operator-sanctioned, self-contained package
documented at `~/.config/opencode-bus-runner/README.md` (launchd `opencode
serve` on 127.0.0.1:4196, pinned session id, mode-600 password). To disable
wakes: remove the script from `COORD_WAKE_ADAPTER_DIR` (wakes stay visibly
queued — never a silent drop) or bootout the serve launchd job.

## Continuity (additive note)

A wake is a HINT, never the obligation — so a woken session must be able to
rebuild its mind from the bus, not from the nudge. The pinned session's
standing orders do: `wake consume` (absorb keyed nudges) → `continuity resume`
(adopt the last snapshot) → `inbox` triage → work → `continuity snapshot` →
presence beat. Idle-listener reaping (Ash, 2026-07-20) applies unchanged: 48h
with no surfaced work → park continuity and stand down the listener (enforced
mechanically on the reference host by `bin/reaper.sh`).

## Security notes

- Server binds 127.0.0.1 only; Basic-auth password is per-install, mode 600.
- The nudge body is a fixed template; the only interpolated values are
  charset-gated (`[A-Za-z0-9_.:@/-]`) and json.dumps-encoded by a fixed python3
  program. No caller byte reaches shell or JSON source.
- The adapter spawns nothing but curl/python3, binds nothing, writes nothing
  but its stdout/stderr line.
