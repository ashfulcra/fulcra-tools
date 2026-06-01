"""Single source of truth for the Attention DurationAnnotation descriptor.

Two paths encode the "Attention" definition and can drift:

  1. CLI bootstrap (fulcra.py ensure_definitions) builds the FULL create
     payload via wire.duration_definition_payload(...).
  2. The daemon resolver (collect_plugin.py ATTENTION_SPEC) hands a
     structural subset to ctx.resolved_definition_id(), which
     fulcra_common.definitions._spec_matches() compares against an existing
     Fulcra def to decide adopt-vs-create.

This module holds the canonical descriptor ONCE and derives both:

  - attention_create_payload(tags) -> the wire create body, and
  - attention_resolver_spec()      -> the structural match-spec, computed as
    a projection of that same payload onto the keys _spec_matches reads.

So the resolver's match-spec can never silently desync from the create
payload's measurement structure: change the measurement_spec in one place
(here, via the wire helper) and both paths move together.

NOTE on creation behaviour (data-correctness): the two paths still CREATE
slightly different defs when each is the first to run, because the resolver
path only sends the projected keys (name + annotation_type +
measurement_spec) and the daemon's create adapter defaults description="",
tags=[] — whereas the CLI path sends this module's description plus the
attention/web tags. Both paths find-or-adopt before creating and adoption
keys only on annotation_type + measurement_spec, so the account converges on
one def either way. This module deliberately single-sources only the
STRUCTURAL match-spec and the create payload's parameters; it does not change
what description/tags the resolver create sends (that would alter what gets
created for existing users).
"""
from __future__ import annotations

from collections.abc import Sequence

from fulcra_common import wire

# The canonical Attention definition parameters — the ONE place these live.
# Tags are intentionally excluded: they are runtime-resolved tag ids (the
# bootstrap path looks up "attention"/"web" tag ids), not a static part of
# the descriptor. value_type and unit are passed through to
# wire.duration_definition_payload; annotation_type ("duration") and
# measurement_type ("duration") are fixed by that wire helper, so they are
# not re-stated here.
ATTENTION_CANONICAL: dict = {
    "name": "Attention",
    "description": "What the user paid attention to (browsing).",
    "value_type": "duration",
    "unit": None,
}

# The keys fulcra_common.definitions._spec_matches() actually compares when
# deciding whether to adopt an existing Fulcra def. The resolver match-spec
# is the create payload projected onto exactly these keys, so projecting is a
# faithful, behaviour-preserving subset. Kept in sync with _spec_matches by
# test_definition_spec.test_resolver_match_keys_are_what_spec_matches_reads.
RESOLVER_MATCH_KEYS: tuple[str, ...] = ("annotation_type", "measurement_spec")


def attention_create_payload(tags: Sequence[str]) -> dict:
    """The FULL Fulcra create body for the Attention duration definition,
    built from the canonical descriptor. `tags` are the resolved tag ids the
    caller wants attached (e.g. the bootstrap path passes [attention, web])."""
    return wire.duration_definition_payload(
        name=ATTENTION_CANONICAL["name"],
        description=ATTENTION_CANONICAL["description"],
        tags=tags,
        value_type=ATTENTION_CANONICAL["value_type"],
        unit=ATTENTION_CANONICAL["unit"],
    )


def attention_resolver_spec() -> dict:
    """The structural match-spec the resolver compares — a projection of the
    canonical create payload onto RESOLVER_MATCH_KEYS. Derived (not re-typed)
    so it can't drift from the create payload's measurement structure. Tags
    are irrelevant to the projection (not a RESOLVER_MATCH_KEY), so any value
    works here."""
    payload = attention_create_payload(tags=())
    return {k: payload[k] for k in RESOLVER_MATCH_KEYS}
