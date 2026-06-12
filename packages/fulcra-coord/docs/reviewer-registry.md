# Reviewer Registry Design

Status: design seed for operator review.

## Problem

Reviewer routing still has one policy surface that is not bus-native:
`FULCRA_COORD_REVIEW_SEED` and `~/.config/fulcra-coord/review-routing.json`.
Those local hints are useful as bootstraps, but they do not move with the bus.
When the canonical reviewer changes host or identity, authors can keep routing
to stale seeds until each local config is updated.

The bus already has the pieces this needs:

- presence records say which agents are live and which capabilities they
  declare;
- role records define durable fleet concepts such as `review`;
- role leases say which live agents currently hold a role;
- role health already reports vacant/contested roles and can escalate vacancies.

The design goal is to make reviewer routing read a machine-readable bus record
first, so a reviewer move is one role claim/release, not a broadcast-and-hope
configuration change.

## Proposal

Use the existing `review` role as the canonical reviewer registry.

The reviewer registry is not a new top-level schema. It is the projection of:

- `roles/review.json` for policy, maintainer, SLA, and instructions;
- `roles/review/leases/<agent>.json` for current holders;
- `presence/<agent>.json` for liveness and capability freshness.

`request-review` should build its candidate pool in this order:

1. Explicit `--candidate-list`, unchanged. This is an operator override.
2. Fresh holders of the canonical `review` role, ordered by presence freshness
   or stable registry order. The lease is a qualifying gate, not the ranking
   signal.
3. Fresh holders of migration aliases such as `reviewer`, normalized to the
   canonical source in diagnostics.
4. Presence agents declaring capability `review`, ordered as today.
5. Existing env/file review seeds, as compatibility fallback only.

That flips local review seeds from "canonical registry" to "legacy bootstrap".
The live bus holder becomes authoritative once the role exists or has leases.

## Naming and Migration

The canonical reviewer role is `review`.

`FULCRA_COORD_REVIEW_ROLE` may override that name for a local fleet, but the
default must be fixed so authors, installers, docs, and diagnostics can agree.
The historical `reviewer` role remains a compatibility alias during migration:

- `connect --role reviewer` and `roles claim reviewer` should continue to work;
- review routing should read both `review` and `reviewer` while reporting
  canonical source `role:review`;
- installers and new docs should emit `--role review`;
- role-health should surface both names during migration, with guidance to
  move live holders to `review`;
- after the fleet has stopped advertising `reviewer`, the alias can become a
  warning-only path.

Day-one adoption must include a bus audit because a syntactically correct role
claim is not useful if the projected lease remains invisible. The live fleet has
already shown this failure mode: both `review` and `reviewer` may appear vacant
even after agents believe they claimed them. Before routing depends on the
registry, verify claim visibility with commands like:

```bash
fulcra-coord roles claim review --agent claude-code:ArcBot:Arc-Code-Review
fulcra-coord connect --agent claude-code:ArcBot:Arc-Code-Review --role review
fulcra-coord roles --format json
```

The audit passes only when the claimant appears as a fresh HELD lease under the
canonical role. If it does not, fix the role lease write path, presence freshness
gate, or view fallback before enabling role-first routing. A vacant projection
must be treated as a deployment blocker, not as evidence that no reviewers are
available.

## Operational Flows

### Move the canonical reviewer

```bash
fulcra-coord roles release review --agent old-agent
fulcra-coord roles claim review --agent new-agent
fulcra-coord connect --agent new-agent --role review
```

No author-local config changes are required. The next `request-review` reads the
fresh holder.

### Seed a fresh fleet

```bash
fulcra-coord roles set review \
  --description "Review code/artifacts for the coordination fleet" \
  --policy shared \
  --sla-hours 24 \
  --maintainer @coord-maintainer
```

Agents then claim the role through install-time role declarations or explicit
connect/roles commands:

```bash
fulcra-coord install-codex --role review
fulcra-coord install-claude-code --can-review --role review
fulcra-coord roles claim review --agent claude-code:ArcBot:Arc-Code-Review
```

`--can-review` remains valid as a lightweight capability declaration, but the
role lease is the durable "canonical reviewer" entry.

### Migrate `reviewer` holders

During the compatibility window, reviewers should claim the canonical role on
their next connect:

```bash
fulcra-coord connect --agent claude-code:ArcBot:Arc-Code-Review \
  --role review \
  --can-review
```

Agents still using `reviewer` should remain routable through the alias, but dry
run output should make the alias visible so maintainers can see what still needs
to move.

## Routing Semantics

`request-review` should prefer fresh role holders over generic review-capable
agents. A stale or missing presence record means the lease does not qualify for
routing, matching role-health behavior.

If the role is vacant:

- route to any live `review` capability holder if one exists;
- otherwise escalate to the human as today;
- role-health separately escalates a stale vacancy to the role maintainer once
  `sla_hours` is exceeded.

If the role is contested:

- for `policy=shared`, all fresh holders are candidates;
- for `policy=exclusive`, still choose a deterministic winner but include the
  contested state in dry-run output and health. Reconcile should not silently
  "fix" the contest by rewriting leases. Exclusive contests should never block a
  review request; they should route predictably while making the governance
  issue loud.

## Artifact-Level Deduplication

The registry fixes who can review. It does not, by itself, stop multiple authors
or sessions from requesting review of the same artifact and minting duplicate
directives.

`request-review` should normalize the artifact identity before writing:

- repository identity, such as `owner/name`;
- artifact reference, such as a pull request URL, PR number, commit SHA, or
  durable document path;
- artifact kind, when known.

Before creating a new review directive, it should search for an existing open
review loop with the same normalized key. Open means routed, proposed, accepted,
in review, waiting, or otherwise non-terminal. Terminal `done`, `abandoned`, and
closed review loops are not deduped because a new review after new changes is
valid.

When an open loop exists, `request-review` should attach the requester or append
a route event to that loop and return the existing directive id. It should not
create another task. Dry-run and JSON output should include an `existing_review`
field so callers can distinguish "new route" from "already routed".

This rule belongs in the write path, not only in reconcile. Reconcile may still
report duplicate historical loops, but it should not be the primary guard
against new duplicate review requests.

## SLA Nudges

The registry does not replace review-loop timers. It gives those timers a
maintainer and a live holder set.

Follow-up implementation should add a bus-native nudge for review loops that
have been routed but not accepted/responded to within the configured window:

- first nudge the assigned reviewer if still live;
- then reroute to another fresh `review` holder;
- then escalate to the `review` role maintainer or human.

This should reuse the existing review route events and directive sub-log, not
create forge-only comments.

## Compatibility

Mixed fleet versions should fail toward the existing behavior:

- old CLIs still route from env/file seeds and review-capable presence;
- new CLIs read role holders first, then fall back;
- `connect --can-review` keeps generic review capability routing alive even
  before any role registry record exists;
- `roles claim review` self-registers a minimal role if needed, so a fresh bus
  can become routable without a separate setup step.

## Implementation Sketch

1. Add a routing helper that loads `review` role holders via `role_ops` and folds
   them with guarded presence liveness. Include `reviewer` as a migration alias
   unless `FULCRA_COORD_REVIEW_ROLE` explicitly selects a different fleet role.
2. Change `_review_pool(author, presence)` to accept optional role candidates
   and order them before generic capability holders.
3. Add the artifact-level dedup guard to `request-review` before new directive
   creation.
4. Keep `FULCRA_COORD_REVIEW_SEED` and `review-routing.json` as fallback, with a
   deprecation note in docs and dry-run output.
5. Extend `request-review --dry-run --format json` to show candidate source:
   `explicit`, `role:review`, `role-alias:reviewer`, `capability:review`, or
   `legacy-seed`, plus `existing_review` when dedup attaches.
6. Add reconcile/health tests for vacant/contested role behavior, routing tests
   for role-holder preference, stale holder exclusion, alias migration, legacy
   fallback, and duplicate open artifact suppression.

## Open Questions

- Should alias support for `reviewer` be time-boxed to a release window, or left
  indefinitely as harmless compatibility?
- What exact artifact canonicalizer should own PR URL, PR number, and commit SHA
  equivalence?
- Which role metadata shape should eventually replace author-specific policy in
  `review-routing.json`? Until then, keep that file as the author-policy
  fallback rather than blocking role-first routing on a larger schema change.
