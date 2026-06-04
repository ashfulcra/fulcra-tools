"""Single-source-of-truth equivalence for the Attention definition spec.

The Attention DurationAnnotation descriptor lives in two paths that can
drift (see definition_spec.py):

  1. The CLI bootstrap create-payload — wire.duration_definition_payload(...)
  2. The resolver match-spec — attention_resolver_spec(), compared by
     fulcra_common.definitions._spec_matches().

These tests pin the invariant that the resolver match-spec is a *projection*
of the canonical create payload onto exactly the keys the resolver matches
on, so a future change to the measurement structure can't desync the two
paths. (The collect plugin is now a relayless informational pointer and no
longer resolves a definition; the spec's single source of truth lives in
definition_spec, where these tests now exercise it directly.)
"""
from __future__ import annotations

from fulcra_attention.definition_spec import (
    ATTENTION_CANONICAL,
    RESOLVER_MATCH_KEYS,
    attention_create_payload,
    attention_resolver_spec,
)
from fulcra_common import wire
from fulcra_common.definitions import _spec_matches

# The resolver match-spec, single-sourced in definition_spec. (Used to be
# re-exported as collect_plugin.ATTENTION_SPEC, before the collect plugin
# became a relayless pointer.)
ATTENTION_SPEC = attention_resolver_spec()


def test_resolver_match_keys_are_what_spec_matches_reads():
    """Guard the projection against _spec_matches drifting. _spec_matches
    compares annotation_type plus the expected measurement_spec fields; if
    it ever starts reading more keys, the projection below stops being
    faithful and this test must be revisited."""
    assert RESOLVER_MATCH_KEYS == ("annotation_type", "measurement_spec")


def test_attention_spec_is_projection_of_create_payload():
    """ATTENTION_SPEC must equal the canonical create payload projected onto
    the keys the resolver matches. Tags differ between the two paths (the
    create payload carries resolved tag ids, the spec carries none), but
    tags are NOT a key _spec_matches reads, so the projection is unaffected.
    Pass arbitrary tags to prove the projection ignores them."""
    payload = wire.duration_definition_payload(
        name=ATTENTION_CANONICAL["name"],
        description=ATTENTION_CANONICAL["description"],
        tags=["tag-attention", "tag-web"],
        value_type=ATTENTION_CANONICAL["value_type"],
        unit=ATTENTION_CANONICAL["unit"],
    )
    projection = {k: payload[k] for k in RESOLVER_MATCH_KEYS}
    assert ATTENTION_SPEC == projection
    assert attention_resolver_spec() == projection


def test_create_payload_helper_matches_wire_directly():
    """attention_create_payload(tags=...) is exactly the wire payload built
    from the canonical descriptor — proving the CLI path is derived, not
    hand-typed."""
    tags = ["tag-attention", "tag-web"]
    assert attention_create_payload(tags) == wire.duration_definition_payload(
        name="Attention",
        description="What the user paid attention to (browsing).",
        tags=tags,
        value_type="duration",
        unit=None,
    )


def test_resolver_spec_matches_a_payload_built_definition():
    """End-to-end: a Fulcra def whose shape is the canonical create payload
    must be accepted by the resolver when matched against ATTENTION_SPEC.
    This is the behaviour the daemon relies on for cross-machine adoption."""
    existing = attention_create_payload(["tag-attention", "tag-web"])
    assert _spec_matches(existing, ATTENTION_SPEC) is True


def test_canonical_descriptor_is_the_known_attention_def():
    """Pin the canonical values so an accidental edit to name/description/
    unit is caught — these define what gets CREATED for every user."""
    assert ATTENTION_CANONICAL == {
        "name": "Attention",
        "description": "What the user paid attention to (browsing).",
        "value_type": "duration",
        "unit": None,
    }
