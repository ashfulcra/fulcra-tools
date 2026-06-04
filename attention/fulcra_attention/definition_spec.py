"""Single source of truth for the Attention DurationAnnotation descriptor.

Two paths encode the "Attention" definition and can drift:

  1. CLI bootstrap (fulcra.py ensure_definitions) builds the FULL create
     payload via wire.duration_definition_payload(...).
  2. The daemon resolver (collect_plugin.py ATTENTION_SPEC) hands a
     structural subset to ctx.resolved_definition_id(), which
     fulcra_common.definitions._spec_matches() compares against an existing
     Fulcra def to decide adopt-vs-create.

This module holds the canonical descriptor ONCE and derives all three:

  - attention_create_payload(tags) -> the wire create body, and
  - attention_resolver_spec()      -> the structural match-spec, computed as
    a projection of that same payload onto the keys _spec_matches reads, and
  - attention_create_extra(resolve_tag) -> the description + resolved tag ids
    the daemon resolver-create path injects (via create_extra) so its fresh
    create carries the same rich fields the CLI create does.

So the resolver's match-spec can never silently desync from the create
payload's measurement structure: change the measurement_spec in one place
(here, via the wire helper) and both paths move together.

NOTE on creation behaviour (data-correctness): both paths now CREATE the
IDENTICAL rich Attention def — same name, description, and attention/web
tags. The CLI path sends this module's create payload directly; the daemon
resolver-create path sends the projected match-spec (name + annotation_type +
measurement_spec) PLUS attention_create_extra() (description + resolved tag
ids) as create_extra, which the resolver merges into the create body. Because
both the description and the tag NAMES come from this module
(ATTENTION_CANONICAL + ATTENTION_DEFINITION_TAG_NAMES), a user onboarded via
the wizard gets the same def as one onboarded via the CLI. create_extra is
applied ONLY on a fresh create — both paths still find-or-adopt by name
first (adoption keys only on annotation_type + measurement_spec), so an
existing "Attention" def is adopted unchanged (no duplicate, no mutation) and
the account converges on one def either way.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

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

# The tag NAMES the canonical Attention definition is created with. The CLI
# bootstrap (fulcra.py ensure_definitions) resolves these to ids and passes
# them to attention_create_payload; the daemon resolver-create path resolves
# the same names (via the daemon's client) and passes the ids as create_extra.
# Single-sourced here so the two create paths attach the SAME tags. These are
# tag NAMES, not ids — ids are account-specific and resolved at runtime.
ATTENTION_DEFINITION_TAG_NAMES: tuple[str, ...] = ("attention", "web")

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


def attention_create_extra(resolve_tag: Callable[[str], str]) -> dict:
    """The extra create-body fields the daemon resolver-create path must
    supply so a wizard-onboarded user gets the SAME rich Attention def the
    CLI bootstrap creates: the canonical description plus the resolved
    attention/web tag ids.

    `resolve_tag` maps a tag NAME to its id on the current account (the
    daemon supplies the same client the resolver uses). Both the
    description and the tag NAMES come from this module, so there is still
    ONE canonical source: change the description or the tag set here and
    both the CLI create and the resolver create move together.

    The returned dict is passed to ``ctx.resolved_definition_id(...,
    create_extra=...)`` and merged into the create POST only on a fresh
    create — never on adoption — so it cannot mutate an existing def."""
    return {
        "description": ATTENTION_CANONICAL["description"],
        "tags": [resolve_tag(name) for name in ATTENTION_DEFINITION_TAG_NAMES],
    }


def attention_resolver_spec() -> dict:
    """The structural match-spec the resolver compares — a projection of the
    canonical create payload onto RESOLVER_MATCH_KEYS. Derived (not re-typed)
    so it can't drift from the create payload's measurement structure. Tags
    are irrelevant to the projection (not a RESOLVER_MATCH_KEY), so any value
    works here."""
    payload = attention_create_payload(tags=())
    return {k: payload[k] for k in RESOLVER_MATCH_KEYS}
