"""State-change triggering for the ROLE VACANT family (coord-boss ruling
`ruling-build-the-mint-guards-now-...-11a4720d`, 2026-09-02).

MEASURED CAUSE. 117 open ROLE VACANT rows carried 12 distinct facts: the title
embeds `{today}`, so every sweep mints a NEW slug and the existing
`transport.read(dst) is None` guard can never match across days. `marker_exists_today`
suppresses re-notification WITHIN a day only, which is why the family grew ~2-6
rows/day fleetwide.

The guard is state-change: while a vacancy row for a role is already open, a later
sweep must not mint a second one for the same role on a later date.
"""
import pytest

from coord_engine import roles


def _title(date, role, kind="lapsed"):
    if kind == "lapsed":
        return (f"ROLE VACANT {date}: {role} lease lapsed past 12h SLA "
                f"(attendance UNVERIFIED)")
    return (f"ROLE VACANT {date}: {role} UNATTENDED past 12h SLA "
            f"— no holder work found")


def test_role_with_an_open_vacancy_row_is_not_minted_again_on_a_later_day():
    open_titles = [_title("2026-09-01", "codex-reviewer")]
    assert roles.vacancy_already_open(open_titles, "codex-reviewer") is True


def test_a_role_with_NO_open_row_still_mints():
    """The guard must not silence the FIRST notice — that one is the useful one."""
    open_titles = [_title("2026-09-01", "codex-coord-inbox")]
    assert roles.vacancy_already_open(open_titles, "codex-reviewer") is False


def test_empty_board_mints():
    assert roles.vacancy_already_open([], "codex-reviewer") is False


def test_both_title_variants_count_as_open():
    """UNATTENDED and lease-lapsed are the same fact about the same role."""
    assert roles.vacancy_already_open(
        [_title("2026-08-30", "build-lane", kind="unattended")],
        "build-lane") is True


def test_role_names_match_EXACTLY_not_by_prefix():
    """A prefix match would let `codex-reviewer` suppress `codex-reviewer-2`.

    Slug-prefix collisions have silently dropped messages on this bus before;
    an identity transform used for suppression must be injective.
    """
    open_titles = [_title("2026-09-01", "codex-reviewer")]
    assert roles.vacancy_already_open(open_titles, "codex-reviewer-2") is False
    assert roles.vacancy_already_open(
        [_title("2026-09-01", "codex-reviewer-2")], "codex-reviewer") is False


def test_unrelated_titles_are_ignored():
    assert roles.vacancy_already_open(
        ["REVIEW REQUEST: pr-682 (codex-reviewer)",
         "OBLIGATION (Ash-ordered 2026-08-30): something"],
        "codex-reviewer") is False


def test_none_input_is_not_treated_as_an_empty_board():
    """UNKNOWN is not 'no open rows'. A listing we could not read must NOT be
    rendered as 'nothing open', because that decision mints a row."""
    with pytest.raises(ValueError):
        roles.vacancy_already_open(None, "codex-reviewer")


# --- the SLUG form: the representation the mint path actually sees -----------

def test_slug_form_is_recognised():
    slug = ("role-vacant-2026-09-02-codex-reviewer-lease-lapsed-past-12h-sla-"
            "attendance-unver")
    assert roles.vacancy_role_of_slug(slug) == "codex-reviewer"
    assert roles.vacancy_already_open([slug], "codex-reviewer") is True


def test_slug_form_unattended_variant():
    slug = "role-vacant-2026-08-30-build-lane-unattended-past-12h-sla-no-holder"
    assert roles.vacancy_role_of_slug(slug) == "build-lane"


def test_slug_role_match_is_exact_not_prefix():
    slug = "role-vacant-2026-09-02-codex-reviewer-2-lease-lapsed-past-12h-sla"
    assert roles.vacancy_role_of_slug(slug) == "codex-reviewer-2"
    assert roles.vacancy_already_open([slug], "codex-reviewer") is False


def test_non_vacancy_slug_is_ignored():
    assert roles.vacancy_role_of_slug("review-request-pr-682-codex-reviewer") is None
    assert roles.vacancy_role_of_slug("role-vacant-nodate-foo-lease-lapsed-x") is None
