# Proposal: audiences + consent levels for the official `fulcra-prefs` skill

**Status:** draft (reeval epic phase-4, item 1) · **Author:** fulcra-prefs-maintainer
**Target venue:** `fulcradynamics/agent-skills` (issue → PR from an `ashfulcra` fork)
**Source of record:** phase-3 realignment verdict (`artifact/2026-07-04-prefs-vault-realignment-verdict.md`, APPROVED)

> This is the issue/PR text we intend to file upstream, staged here for codex-prefs
> review before it leaves the repo. No code ships from this doc — it proposes
> conventions and an optional schema field for the official annotation-based
> preference skill.

## Context

The official `fulcra-prefs` skill stores preferences as `MomentAnnotation`
"User Preference" records shaped `{key, value, scope, strength}` with newest-wins
resolution. The `fulcra-tools` package of the same name (a reference implementation
that predates the official skill) additionally supports **audiences**, **consent
levels**, session-start **injection**, and **solver participation**. The name
collision is resolved in the official skill's favor; this proposal upstreams the
two extensions that are generally useful and schema-compatible — audiences and
consent — so the official skill can serve multi-agent and disclosure-gated use
cases without a separate package.

The design goal is **additive and backward-compatible**: a record written without
these fields behaves exactly as today.

## Two orthogonal dimensions: applicability vs. authorization

The key correction over an earlier sketch: **applicability** (*where* a preference
applies) and **authorization** (*who* it may be disclosed to) are independent and
must not be collapsed. `scope` keeps its current meaning (applicability:
`global`, `platform:<name>`). Audience authorization rides in a **separate,
audience-bearing field** so a record can be both `platform:claude-code` *and*
disclosable to `ea-agent` — impossible if we overloaded `scope`.

## Extension 1 — `consent`: a list of per-audience grants (one new optional field)

```
{ "key": "dining.cuisine.thai", "value": {"liked": true},
  "scope": "platform:claude-code", "strength": 0.8,
  "consent": [ {"audience": "ea-agent", "level": "read",  "granted_at": "<iso8601>"},
               {"audience": "travel-bot","level": "solve", "granted_at": "<iso8601>"} ] }
```

- `consent` is an optional **array of grants**, each naming exactly one `audience`,
  a `level`, and `granted_at`. A grant authorizes disclosure of *this record* to
  *that named audience* only — no wildcard, no implicit "all audiences".
- **Absent or empty `consent` ⇒ private** (never disclosed to any audience) — the
  backward-compatible default; existing records carry no `consent` and stay private.
- `scope` still governs applicability independently: a grant does not change which
  platform/global view a record belongs to.

### Authorization matrix (fail-closed)

Disclosing a record's value to audience `A` for purpose `P` is permitted **iff**
`consent` contains a grant whose `audience == A` and whose `level` authorizes `P`:

| grant `level` | authorizes purpose `read` | authorizes purpose `solve` |
|---|---|---|
| `read`  | ✅ | ❌ |
| `solve` | ✅ (solve ⊇ read) | ✅ |

- `solve` is the strict superset (matches the reference package: a `solve` grant is
  visible to both `read` and `solve` purposes; a `read` grant only to `read`).
- **Everything else denies:** no matching grant, an unknown/unrecognized `level`, a
  malformed grant (missing `audience`/`level`), or absent `consent` ⇒ **no
  disclosure**. Compilers MUST fail closed on anything they don't understand rather
  than default-allow.

`strength` is unchanged and already expresses preference vs. aversion.

## Extension 2 — disclosure records are audit events, not preferences

Each disclosure (a compile *for* an audience) emits a **disclosure record** — but
it must **not** enter ordinary preference synthesis. Under the official skill's
single `User Preference` stream, an audit entry keyed like a preference would
otherwise be folded into newest-wins resolution and pollute working context.

Proposal: disclosure records carry a distinct marker (a reserved kind/type, e.g.
`kind: "consent.disclosure"`, or a dedicated data-type) and the compiler
**excludes** them from preference synthesis while retaining them as the audit
stream (the "Privacy Ledger"). The exclusion rule is part of the contract, not an
implementation detail — a reader that folds disclosure records into preferences is
non-conformant.

## Injection — a convention, not a CLI verb

The reference package injects a compiled preference block at session start via a
CLI (`inject --platform`). The official surface has no such verb, and the nearest
official pattern is **session-start prose** (cf. `fulcra-situational-awareness`).
We propose documenting preference injection as a **session-start convention** in
the skill text: at session start, read the compiled preference view and prepend it
to working context. This keeps the official skill CLI-free (prose over
`fulcra-api`) while giving agents the same load-at-start behavior.

## What stays package-side (explicitly out of scope for this proposal)

- **Solver participation** (`get --for <aud> --purpose solve`, the ranking/veto
  engine) stays in the `fulcra-tools` package until the sharing primitive ships
  (parked per operator direction 2026-06-17).
- The compiler's half-life decay math and deterministic conflict resolution remain
  the reference implementation's concern; this proposal does not ask the official
  skill to adopt the decay model, only the two disclosure-facing extensions.

## Compatibility summary

| Change | Wire impact | Old readers |
|---|---|---|
| `consent` grants array | one optional array field | absent/empty ⇒ private (today's behavior) |
| `scope` semantics | unchanged (applicability only) | unaffected |
| disclosure records | reserved kind/type, excluded from synthesis | must skip them (part of the contract) |
| session-start injection | docs only | n/a |

Every change is additive and fail-closed: a reader that understands none of it sees
today's private-by-default behavior; a reader that half-understands it must deny
disclosure rather than guess.

## Open questions for upstream maintainers

1. `consent.level` as a closed enum (`read`/`solve`) in the schema, or an open
   string with documented well-known values + fail-closed on unknown? (We lean
   closed enum for safety.)
2. Marker for disclosure/audit records: a reserved `kind` value on the shared
   preference stream, or a dedicated data-type? Either works provided synthesis
   excludes them by contract.
3. Venue: issue-first for discussion, then PR against the skill's `SKILL.md` +
   schema doc.
