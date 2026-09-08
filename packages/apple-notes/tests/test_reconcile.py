"""Three-way classification for the two-way sync."""
from __future__ import annotations

from fulcra_apple_notes import render
from fulcra_apple_notes.reconcile import (
    Change, Status, classify, parse_vault_note, summarize)

SYNCED_BODY = "hello world"
SYNCED_HASH = render.content_hash(SYNCED_BODY)
STATE = {"path": "notes/apple/n-1.md", "hash": SYNCED_HASH,
         "modified": "2026-01-01T00:00:00+00:00", "title": "N"}


def vault_file(body: str, *, fence: bool = True) -> str:
    inner = (f"{render.OPEN_FENCE}\n{body}\n{render.CLOSE_FENCE}"
             if fence else body)
    return f"---\ntitle: N\napple-uuid: u1\n---\n\n{inner}\n\n## Log\n- x\n"


def test_parse_extracts_frontmatter_and_fenced_body():
    fields, body = parse_vault_note(vault_file("some text"))
    assert fields["apple-uuid"] == "u1"
    assert body == "some text"


def test_parse_returns_none_body_when_the_fence_is_gone():
    _fields, body = parse_vault_note(vault_file("text", fence=False))
    assert body is None


def test_quoted_frontmatter_values_are_unquoted():
    fields, _ = parse_vault_note('---\ntitle: "Re: budget"\n---\n\nx\n')
    assert fields["title"] == "Re: budget"


def test_nothing_changed_is_unchanged():
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified=STATE["modified"],
                 vault_text=vault_file(SYNCED_BODY), state_entry=STATE)
    assert c.status is Status.UNCHANGED


def test_edit_in_the_vault_only_is_a_writeback_candidate():
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified=STATE["modified"],
                 vault_text=vault_file("I edited this"), state_entry=STATE)
    assert c.status is Status.VAULT_EDITED


def test_change_in_apple_only_is_a_normal_forward_sync():
    c = classify(uuid="u1", apple_hash="different",
                 apple_modified="2026-02-02T00:00:00+00:00",
                 vault_text=vault_file(SYNCED_BODY), state_entry=STATE)
    assert c.status is Status.APPLE_CHANGED


def test_both_sides_changed_is_a_conflict_not_a_silent_overwrite():
    c = classify(uuid="u1", apple_hash="different",
                 apple_modified="2026-02-02T00:00:00+00:00",
                 vault_text=vault_file("I edited this too"), state_entry=STATE)
    assert c.status is Status.CONFLICT


def test_apple_mod_date_moving_alone_still_counts_as_an_apple_change():
    # Body hash equal but mod date moved: formatting-only edits and
    # attachment changes move the date without changing our rendered text.
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified="2026-03-03T00:00:00+00:00",
                 vault_text=vault_file(SYNCED_BODY), state_entry=STATE)
    assert c.status is Status.APPLE_CHANGED


def test_a_hand_restructured_file_is_treated_as_edited_never_unchanged():
    # Overwriting it would discard the human's restructuring.
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified=STATE["modified"],
                 vault_text=vault_file(SYNCED_BODY, fence=False),
                 state_entry=STATE)
    assert c.status is Status.VAULT_EDITED
    assert "restructured" in c.detail


def test_note_absent_from_apple_is_reported_as_deleted():
    c = classify(uuid="u1", apple_hash=None, apple_modified=None,
                 vault_text=vault_file(SYNCED_BODY), state_entry=STATE)
    assert c.status is Status.DELETED_IN_APPLE


def test_note_with_no_state_is_new():
    c = classify(uuid="u9", apple_hash="h", apple_modified="m",
                 vault_text=None, state_entry=None)
    assert c.status is Status.NEW_IN_APPLE


def test_vault_file_deleted_is_reported_not_silently_recreated():
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified=STATE["modified"],
                 vault_text=None, state_entry=STATE)
    assert c.status is Status.MISSING_IN_VAULT


def test_summarize_counts_every_status():
    counts = summarize([
        Change(uuid="a", status=Status.UNCHANGED),
        Change(uuid="b", status=Status.VAULT_EDITED),
        Change(uuid="c", status=Status.VAULT_EDITED),
    ])
    assert counts["unchanged"] == 1 and counts["vault_edited"] == 2
    assert counts["conflict"] == 0


def test_unfetched_but_fresh_vault_file_is_not_mistaken_for_a_deletion():
    # The mtime pre-filter skips downloading unchanged files; without the
    # flag the absent body would read as "the vault file is gone".
    c = classify(uuid="u1", apple_hash=SYNCED_HASH,
                 apple_modified=STATE["modified"], vault_text=None,
                 state_entry=STATE, assume_vault_unchanged=True)
    assert c.status is Status.UNCHANGED


def test_unfetched_vault_file_still_reports_an_apple_side_change():
    c = classify(uuid="u1", apple_hash="moved",
                 apple_modified="2026-05-05T00:00:00+00:00", vault_text=None,
                 state_entry=STATE, assume_vault_unchanged=True)
    assert c.status is Status.APPLE_CHANGED


def test_hash_observation_classifies_without_retaining_private_body():
    from dataclasses import asdict
    import json
    from fulcra_apple_notes.reconcile import observe_vault, classify_observation

    observed = observe_vault(vault_file("Private synthetic edited body"))
    assert "Private synthetic edited body" not in json.dumps(asdict(observed))
    change = classify_observation(
        uuid="u1", apple_hash=SYNCED_HASH, apple_modified=STATE["modified"],
        vault=observed, state_entry=STATE)
    assert change.status is Status.VAULT_EDITED


def test_hash_observation_preserves_missing_file_and_missing_fence_distinction():
    from fulcra_apple_notes.reconcile import observe_vault, classify_observation

    for text, expected in [(None, Status.MISSING_IN_VAULT),
                           (vault_file("text", fence=False), Status.VAULT_EDITED)]:
        change = classify_observation(
            uuid="u1", apple_hash=SYNCED_HASH, apple_modified=STATE["modified"],
            vault=observe_vault(text), state_entry=STATE)
        assert change.status is expected
