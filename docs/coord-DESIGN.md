# coord — deterministic coordination add-ons for fulcra-agent-teams

Thirteen `fulcra-agent-*` skills that layer durable multi-agent coordination onto the official
`fulcra-agent-teams` OKF-markdown convention, backed by one shared stdlib-only CLI (`coord-engine`),
invoked as the installed binary (`coord-engine …` — it is not on PyPI, so `uv tool run` will not
resolve it; see the [quickstart](coord/GET-ON-THE-BUS.md) for the install).

## The design rule

**Prose for judgment, code for folds.** Anything that requires two agents to independently reach
the SAME conclusion from many files — task boards, role vacancy, review verdicts, presence
liveness, ask queues — is a deterministic engine command, never a skill instruction. Skills carry
the judgment: when to snapshot, what to escalate, how to phrase an ask. This split exists because
prose folds drift: two agents eyeballing timestamps disagree, and coordination built on
disagreement heals nothing.

## Tier framing

- **`fulcra-agent-teams` = base tier.** Unchanged. Shared OKF-markdown space, inbox lifecycle,
  freeform progress notes. Everything here layers ON it; nothing replaces it.
- **coord skills = pro tier, individually optional.** Each skill installs alone and degrades
  gracefully when its neighbors are absent. Adopt presence without roles, tasks without operator.

## The skills (wave = proposed upstream sequencing)

| Skill | Adds | Wave |
|---|---|---|
| fulcra-agent-presence | heartbeat shards; live/idle/stale fold; broadcast roster | 1 |
| fulcra-agent-roles | claimable role leases; HELD/VACANT/CONTESTED; SLA escalation; session-nonce double-acting guard | 1 |
| fulcra-agent-continuity | structured snapshots (objective/decisions/next/questions); deterministic resume brief | 1 |
| fulcra-agent-review | verdict handshake; APPROVED/CHANGES/PENDING fold; required-reviewer gating | 1 |
| fulcra-agent-directives | tell/broadcast/remind/handoff; per-agent inbox with acks | 1 |
| fulcra-agent-health | doctor preflight; fleet health fold (which hosts heal, who went dark) | 1 |
| fulcra-agent-reconcile | engine-owned `task/index.md` + `log.md`; status/board/needs-me/search | 2* |
| fulcra-agent-tasks | typed task lifecycle; validated state machine; done-requires-evidence | 2 |
| fulcra-agent-forge | GitHub PR state mirrored as review evidence; auto-approve on merge | 2 |
| fulcra-agent-automation | launchd/cron heartbeat + inbox listener installers (hardened) | 2 |
| fulcra-agent-operator | waiting-on-operator asks fold; atomic answer verb; courier conventions | 2 |

\* reconcile is the one semantic conversation: it makes the task index engine-owned, so wave 2
includes a small amendment to fulcra-agent-teams' SKILL.md ("if reconcile is installed, do not
hand-edit the index"). Its value is NOT change detection (teams already has `data-updates`); it is
that **two agents always agree on the fold**.

## Identity doctrine (adopted 2026-07-04)

Agent identity = the ROLE a session acts as (`FULCRA_COORD_AGENT=coord-maintainer`), never a
host/cwd-derived string (multi-session collision, hostname rot; the engine's derived-id fallback
exists only for unconfigured sessions and is deprecated as an address). The role's exclusive lease makes
different-id contention visible (CONTESTED); a per-session nonce in the lease catches the same-id
blind spot leases cannot see. Session/host details are metadata, not address.

## Engine facts

- Python, stdlib-only, zero runtime deps; transport shells to `fulcra-api file` (swap-in point if
  folded into fulcra-api — that fold replaces the subprocess layer with internal API calls and
  should be sized WITH the API team, it is not mechanical).
- Installs from a git tag via `uv tool install` (verified by live tag-builds at v0.4.0 and v1.0.1;
  the current release is tagged v1.3.0).
- Never-raise CLI discipline: advisory features degrade to stderr notes; exit codes are contracts.
- 200 tests; every stateful fold has transport-injected tests.

## Evidence pack

- **Review lineage**: adversarially reviewed throughout — Claude Opus per-PR with fix→re-review
  loops (verdicts on the early PRs' threads), independent Codex verdicts recorded as done-evidence
  on the coordination bus itself (`team/fulcra` task docs — the review-handshake skill eating its
  own dog food) plus post-merge review waves; the upstream plan passed a Codex adversarial review
  (2026-07-04).
  Review rounds routinely caught real defects (test-suite home-dir pollution, prose/engine
  contradictions, a query over-match) before merge.
- **Live migration**: 139/139 tasks migrated from the predecessor system, acceptance suite green,
  identical-ids policy so in-flight assignments survived.
- **Live operator-loop result**: first day deployed, the asks fold surfaced a real blocker buried
  for 26 days; the operator answered; the atomic answer verb handed it back — full round trip on
  production data.
- **Incident-hardened**: a catalog shape change in fulcra-api 0.1.35 exposed a matching fragility
  in the predecessor, producing a duplicate-timeline incident; the postmortem produced dual-shape matchers, cache pinning, and the
  Track-3 platform asks (file JSON/version-id/batch-read, record-write verbs, archived-type flags)
  — each ask carries its incident evidence.
- **Fleet-tested**: multi-host adoption with per-agent ack shards (born from a real broadcast that
  one agent closed for everyone), hardened launchd installers (validated inputs, plist lint,
  cksum-suffixed keys).

## What does NOT go upstream

Migration tooling (one-shot, done), the predecessor's 0.15.x fixes (sunsetting), host-local cache
pins, and `docs/proposals/` history (this file is the summary).
