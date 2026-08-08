# Identity alias resolution — design

An agent that is renamed does not take its obligations with it. Work addressed
to the old identity keeps arriving at a name nobody reads, and every fold that
keys on identity stops seeing it. This document specifies the alias table that
lets a fold join those buckets, and — more importantly — the boundary it must
never cross.

Status: design. No implementation yet. The normative sections below are agreed
and are not open for reinterpretation at build time.

## The failure it exists to fix

A renamed agent's obligations strand across its former identities. In the
incident that produced this design, three former identities held roughly 43
obligations between them, and among them were **daily role-vacancy alarms
delivered to the identity that had lapsed** — an alarm about an absence,
addressed to the absent party.

That second symptom is NOT an alias problem and is fixed separately: a vacancy
notice is addressed to the role registry's `maintainer:` field, so a role whose
maintainer is its own lapsed holder closes the loop with one field, not with a
rename. See the closed-loop check in `escalate`. It is mentioned here only so
nobody waits on this design to fix it.

What remains for aliases is the honest part: obligations addressed to a name
that is no longer read.

---

## NORMATIVE: aliases resolve READS, never AUTHORITY

> **A fold that changes what someone is ALLOWED to do must resolve the literal
> identity. Aliases apply only to folds that decide what someone should SEE.**

An alias entry is a machine-readable claim that two identities are the same
principal. Identity is used as authority in at least three places:

1. **Verdict attribution.** A verdict is keyed by the requirement token encoded
   in the ACL-controlled *filename*, specifically so a file `mallory.md`
   claiming `reviewer: alice` cannot shadow alice's verdict. If the tally
   consults an alias table, then whoever can write the table can make one
   agent's verdicts count as another's, and the comment explaining why we key on
   the filename becomes false.
2. **Role leases.** An exclusive lease is a claim that *this* identity holds the
   role. Alias-folding lease holders lets two identities satisfy one exclusive
   lease.
3. **Any may-this-agent-act decision**, present or future.

Permitted (read-side): `needs-me`, `obligations`, board census, digest,
inbox folds.
Forbidden (authority): verdict tally, lease holding, and anything gating an
action.

Without this line the alias table is a privilege-escalation primitive with a
convenience story on top.

## NORMATIVE: alias debt is visible and is NOT ordinary work

> **A row joined in from an alias must be rendered as UNDISCHARGEABLE by the
> canonical identity, with the remediation named. It is never presented as
> work the canonical can simply do.**

This is the gap the first draft left, and it follows directly from the
authority boundary rather than contradicting it. Concretely: `needs-me` joins a
review required from retired alias `A` into canonical `B`'s view — correct, `B`
should see it. But the verdict tally refuses alias resolution, as it must, so a
verdict filed as `B` does not satisfy required token `A`, and filing as `A`
would cross the boundary. `B` is shown an obligation with no legal move.

So the read side must not lie about actionability:

```
[P1] review pr-123 — required from `A` (retired -> B)
     ALIAS DEBT: you cannot discharge this. The requirement names a retired
     identity and a verdict from B will not satisfy it.
     Remediation: the requester re-requests naming B, or reassigns.
```

The row carries `alias_debt: true` and a `remediation` field in `--json`.
Counters that report "open obligations" must be able to exclude it, or every
dashboard shows permanent work nobody can finish.

**Rule of thumb for the whole design:** an alias join may change what you can
SEE and must never change what you can DO — including making something look
doable that is not.

## NORMATIVE: one canonical per alias; many aliases per canonical is EXPECTED

> **The map is a FUNCTION from alias to canonical, and acyclic. Two canonicals
> for one alias is a config error the loader REFUSES. A canonical that is
> itself an alias is a config error the loader REFUSES — chains are never
> resolved transitively.**

Transitive resolution is how two live agents quietly become one. A loader that
follows `A -> B -> C` will, the first time someone adds a row carelessly, merge
two agents that were never the same principal, and the merge is invisible
because every fold downstream reports a single tidy identity.

The loader fails loudly on: an alias that is also a canonical, a cycle, or two
canonicals for one alias. Refusing a malformed table is correct — a coordination
store with no alias table behaves exactly as it does today.

**"Injective" was the wrong word in the first draft and is withdrawn.** Strict
injectivity would forbid several retired identities mapping to one canonical —
which is exactly the motivating case, where one agent was renamed more than
once. Many aliases to one canonical is REQUIRED. The constraint that matters is
that the map is a function (one canonical per alias) and acyclic; those are what
the loader enforces.

## NORMATIVE: a retired id is burned

> **Once `X -> Y` exists, `X` may never again be a live identity.**

If `X` can be reused, every historical row addressed to `X` silently
reattributes to whoever holds it next. Reuse is not a policy choice to be made
per-case; it is a defect in the record.

---

## Shape

The table is team data, not engine code, so it lives on the team's store beside
the tag registry rather than in this repo:

```
_coord/bus-v3/aliases.json
```

```json
{
  "schema": "coord.aliases.v1",
  "aliases": [
    {
      "alias": "<retired identity>",
      "canonical": "<current identity>",
      "retired_at": "2026-08-08T00:00:00Z",
      "reason": "renamed when the host was rebuilt"
    }
  ]
}
```

`retired_at` and `reason` are REQUIRED. An alias with no provenance is
indistinguishable from a typo somebody enshrined, and the next reader cannot
tell which they are looking at.

## Behaviour

**Read folds** join alias buckets into the canonical and mark the join:

```
[P1] some task title  (via alias <retired identity>)
```

The marker is not decoration. It must ride in `--json` as well as the text
render — a fold that is honest only in the human view is dishonest to every
automated reader, which is most of them here.

**Write paths** (`tell`, task assignment) warn and offer the canonical; they do
NOT rewrite silently. A silent rewrite is how a typo becomes an invisible
bucket, which is the failure this whole design is about. The write is refused
or annotated, never quietly redirected.

**Validation** at write time accepts an assignee that appears in the tag
registry OR the alias table, and prints the canonical it would have used. This
also catches the sigil-typo class (`@name` instead of `name`) that strands work
under a name nobody will ever query.

**`doctor`** reports alias buckets, and reports **zero** explicitly. "No
stranded buckets" and "the check did not run" must not look the same — that
equivalence is the failure mode behind most of the incidents this coordination
layer has logged.

## What this design does not do

- It does not merge presence, leases, or verdicts. See the first normative
  section.
- It does not decide who may write the table. That is an operator question and
  the answer belongs with whoever controls the store's ACLs, not here.
- It does not backfill. Existing stranded obligations are joined by the read
  folds once the table exists; nothing rewrites history.
