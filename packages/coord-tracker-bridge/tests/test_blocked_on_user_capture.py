"""Which human a row is blocked on must survive the trip into the projection.

The engine has carried this all along — `later --on-user <name>` writes
`blocked_on: user:<name>`, `query.blocked_on_human` folds it — and this bridge
dropped it on the floor. That is why the first "blocked on me" view had to
hardcode one person's name, and why an answer carried back from Linear would
have been recorded under a single global handle no matter who wrote it.

Only the TYPED form is trusted. The engine's fuzzy fallbacks (a `needs:human`
tag, a blocked row whose assignee equals the caller's configured human, a bare
name matched against membership) resolve against that caller's defaults;
reimplementing them here would be a second interpretation free to disagree.
Disagreement about which person owns a decision is the worst thing for this
package to guess at, so an untyped block stays unresolved.
"""

from __future__ import annotations

import pytest

from coord_tracker_bridge.source import EngineSourceAdapter

resolve = EngineSourceAdapter._blocked_on_user


def test_typed_user_block_resolves_to_that_person() -> None:
    assert resolve({"blocked_on": "user:ash"}) == "ash"
    assert resolve({"blocked_on": "user:liz"}) == "liz"


def test_whitespace_is_trimmed() -> None:
    assert resolve({"blocked_on": "  user:kristina  "}) == "kristina"


@pytest.mark.parametrize("row", [
    {},
    {"blocked_on": None},
    {"blocked_on": ""},
    {"blocked_on": "user:"},
    {"blocked_on": "user:   "},
])
def test_absent_or_empty_is_unresolved(row) -> None:
    """None is 'we do not know', never 'the default human'."""

    assert resolve(row) is None


@pytest.mark.parametrize("row", [
    {"blocked_on": "ash", "tags": ["needs:human"]},
    {"blocked_on": "coord-boss"},
    {"blocked_on": "waiting on someone"},
    {"tags": ["needs:human"], "assignee": "human", "status": "blocked"},
])
def test_untyped_blocks_are_unresolved_not_guessed(row) -> None:
    """An inferred name would attribute somebody's decision to the wrong person."""

    assert resolve(row) is None


def test_a_person_named_like_the_prefix_is_not_mangled() -> None:
    assert resolve({"blocked_on": "user:user:odd"}) == "user:odd"
