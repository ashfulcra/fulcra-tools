"""Old source-only success facts cannot prove another destination's delivery."""
from dataclasses import replace

import pytest

from fulcra_gmail.cursors import CursorStore
from fulcra_gmail.delivery_scope import delivery_root
from fulcra_gmail.ledger import Ledger, LedgerEntry
from fulcra_gmail.rules import parse_rules

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"


def rule(actions=("file", "relay")):
    return parse_rules([{"id": "synthetic", "version": 1, "name": "Synthetic",
        "match": "in:inbox", "actions": list(actions), "relay_to": "synthetic-agent"}])[0]


def test_scope_captures_fulcra_principal_and_actual_action_destination(tmp_path):
    selected = rule()
    original = delivery_root(ACCOUNT, selected, "synthetic-team", root=tmp_path)
    assert original == delivery_root(ACCOUNT, selected, "synthetic-team", root=tmp_path)
    assert original != delivery_root(OTHER, selected, "synthetic-team", root=tmp_path)
    assert original != delivery_root(ACCOUNT, selected, "other-team", root=tmp_path)
    assert original != delivery_root(ACCOUNT, replace(selected, relay_to="other-agent"), "synthetic-team", root=tmp_path)
    # An irrelevant relay setting cannot invalidate a file-only delivery fact.
    file_only = rule(("file",))
    assert delivery_root(ACCOUNT, file_only, "one", root=tmp_path) == delivery_root(ACCOUNT, file_only, "two", root=tmp_path)


def test_source_only_legacy_and_other_destinations_never_seed_success_or_cursor(tmp_path):
    selected = rule()
    roots = [tmp_path, delivery_root(ACCOUNT, selected, "team", root=tmp_path),
        delivery_root(OTHER, selected, "team", root=tmp_path)]
    entry = LedgerEntry.file_done(account_id="synthetic-source", message_id="synthetic-message",
        rule_id=selected.id, rule_version=selected.version, sha256="synthetic-hash", destination="/collect/gmail/synthetic")
    legacy = Ledger("synthetic-source", root=roots[0])
    legacy.append(entry)
    CursorStore("synthetic-source", root=roots[0]).set(selected.id, selected.version, 123)
    assert not Ledger("synthetic-source", root=roots[1]).entries()
    assert CursorStore("synthetic-source", root=roots[1]).get(selected.id, selected.version) is None
    Ledger("synthetic-source", root=roots[1]).append(entry)
    CursorStore("synthetic-source", root=roots[1]).set(selected.id, selected.version, 456)
    assert not Ledger("synthetic-source", root=roots[2]).entries()
    assert CursorStore("synthetic-source", root=roots[2]).get(selected.id, selected.version) is None
    assert legacy.entries() == [entry.to_dict()]


@pytest.mark.parametrize("account", [None, "", "not-a-uuid", "../other-account"])
def test_unknown_destination_identity_cannot_create_a_scope(tmp_path, account):
    with pytest.raises(ValueError):
        delivery_root(account, rule(), "team", root=tmp_path)
    assert not list(tmp_path.iterdir())
