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

## Extension 1 — audiences (reuse `scope` as the seam)

The official schema already carries `scope` (e.g. `global`, `platform:<name>`).
Audiences are the same idea one step further: *who* a preference may be disclosed
to, not just *where* it applies. We propose treating an `audience:<name>` scope
value as a first-class, well-known scope form:

```
{ "key": "dining.cuisine.thai", "value": {"liked": true},
  "scope": "audience:ea-agent", "strength": 0.8 }
```

Resolution rule (additive): when compiling a view *for* an audience, records whose
scope is `global`, the active `platform:*`, **or** `audience:<that audience>`
participate; audience-scoped records for other audiences are excluded. Skills that
don't know about audiences simply never emit or request them and see today's
behavior. No new field is required for this extension — it is a **convention over
the existing `scope`**, which keeps the wire schema unchanged.

## Extension 2 — consent tier (one new optional field)

Disclosure to an audience should be *logged* and *scoped by intent*. We propose a
single optional field, `consent`, on the preference record:

```
{ "key": "dining.cuisine.thai", "value": {"liked": true},
  "scope": "audience:ea-agent", "strength": 0.8,
  "consent": {"level": "read", "granted_at": "<iso8601>"} }
```

- `level` ∈ `{read, solve}` (extensible). `read` = the value may be disclosed to
  the audience; `solve` = the value may be used as an input to a group decision but
  not necessarily surfaced verbatim.
- Absent `consent` ⇒ the record is private (never disclosed to any audience),
  exactly today's default. This preserves backward compatibility: existing records
  are private-by-default.
- Each **disclosure** (a compile *for* an audience) emits its own
  `consent.disclosure.<audience>` preference record — the audit trail is itself the
  record stream (the "Privacy Ledger"), needing no separate store.

`strength` is unchanged and already expresses preference vs. aversion, so no change
there.

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
| `audience:<name>` scope | none (convention over `scope`) | ignore unknown scope → treat as non-matching |
| `consent` field | one optional object | absent ⇒ private (today's behavior) |
| session-start injection | docs only | n/a |

## Open questions for upstream maintainers

1. Is `audience:<name>` an acceptable well-known `scope` form, or would you prefer a
   dedicated `audience` field? (We lean on `scope` to avoid a schema change.)
2. Should `consent.level` be a closed enum in the schema or an open string with
   documented well-known values?
3. Venue confirmation: issue-first for discussion, then PR against the skill's
   `SKILL.md` + schema doc.
