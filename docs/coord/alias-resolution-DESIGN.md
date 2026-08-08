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

## NORMATIVE: the map is injective and acyclic

> **One canonical per alias. A canonical that is itself an alias is a config
> error the loader REFUSES — it is never resolved transitively.**

Transitive resolution is how two live agents quietly become one. A loader that
follows `A -> B -> C` will, the first time someone adds a row carelessly, merge
two agents that were never the same principal, and the merge is invisible
because every fold downstream reports a single tidy identity.

The loader fails loudly on: an alias that is also a canonical, a cycle, or two
canonicals for one alias. Refusing a malformed table is correct — a coordination
store with no alias table behaves exactly as it does today.

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
