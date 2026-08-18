"""The rails must REFUSE, not warn — that is the whole difference between a
rule in a plan doc and a rule in the code."""
import pytest

from coord_mesh import safety

GOOD_UID = "a24a9667-c2c6-4bbf-9a0f-36ea0afcb521"


def test_named_uid_accepts_a_real_uuid():
    assert safety.require_named_uid(GOOD_UID) == GOOD_UID
    assert safety.require_named_uid(f"  {GOOD_UID}  ") == GOOD_UID


@pytest.mark.parametrize("bad", [
    "", "   ", None,
    "*",                       # the wildcard that must never mint a share
    "everyone", "coord-boss",  # a name is not a uid
    "a24a9667",                # truncated
    "a24a9667-c2c6-4bbf-9a0f-36ea0afcb52",   # one char short
    "zzzzzzzz-c2c6-4bbf-9a0f-36ea0afcb521",  # non-hex
])
def test_named_uid_refuses_everything_else(bad):
    with pytest.raises(safety.SafetyViolation):
        safety.require_named_uid(bad)


def test_share_all_is_refused_anywhere_in_argv():
    """Checked on the argv actually being executed — including a flag that
    arrived from config or a caller's extra-args, not just a literal."""
    with pytest.raises(safety.SafetyViolation):
        safety.refuse_share_all(["share", "create", "--share-all", "--user-id", GOOD_UID])
    with pytest.raises(safety.SafetyViolation):
        safety.refuse_share_all(["share", "create", "--user-id", GOOD_UID, " --share-all "])


def test_share_all_allows_a_scoped_create():
    safety.refuse_share_all(
        ["share", "create", "--name", "mesh", "--data-type", "MomentAnnotation/x",
         "--file", "reports/", "--user-id", GOOD_UID])


@pytest.mark.parametrize("verb", ["delete", "leave", "revoke", "DELETE", " Leave "])
def test_destructive_verbs_are_refused(verb):
    with pytest.raises(safety.SafetyViolation):
        safety.refuse_destructive(verb)


@pytest.mark.parametrize("verb", ["create", "list-incoming", "list-outgoing", "update"])
def test_non_destructive_verbs_pass(verb):
    safety.refuse_destructive(verb)


def test_violation_messages_say_which_rail_and_why():
    """A refusal a human cannot act on gets worked around."""
    with pytest.raises(safety.SafetyViolation, match="named-uid rail"):
        safety.require_named_uid("nope")
    with pytest.raises(safety.SafetyViolation, match="scoped shares only"):
        safety.refuse_share_all(["--share-all"])
    with pytest.raises(safety.SafetyViolation, match="operator-only"):
        safety.refuse_destructive("delete")
