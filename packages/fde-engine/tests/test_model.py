"""Phase machine + engagement doc model."""

from fde_engine import model


def test_phases_are_the_seven_from_the_spec_in_order():
    assert model.PHASES == [
        "intake", "interview", "architecture", "plan",
        "prototype", "build", "retro",
    ]


def test_forward_transitions_are_valid():
    for cur, nxt in zip(model.PHASES, model.PHASES[1:]):
        assert model.valid_transition(cur, nxt), f"{cur} -> {nxt} should be valid"


def test_prototype_findings_can_reopen_architecture_and_plan():
    assert model.valid_transition("prototype", "architecture")
    assert model.valid_transition("prototype", "plan")


def test_skipping_phases_is_invalid():
    assert not model.valid_transition("intake", "architecture")
    assert not model.valid_transition("interview", "build")
    assert not model.valid_transition("retro", "intake")


def test_unknown_phase_is_invalid():
    assert not model.valid_transition("intake", "shipping")
    assert not model.valid_transition("nope", "interview")


def test_slugify_normalizes_titles():
    assert model.slugify("Sourdough Coach: The App!") == "sourdough-coach-the-app"
    assert model.slugify("  ---  ") == "engagement"
