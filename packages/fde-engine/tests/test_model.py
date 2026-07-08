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
    # Self-transitions are deliberately invalid: every transition appends to
    # phase_history, and a no-op entry would pollute the record.
    assert not model.valid_transition("plan", "plan")


def test_unknown_phase_is_invalid():
    assert not model.valid_transition("intake", "shipping")
    assert not model.valid_transition("nope", "interview")


def test_slugify_normalizes_titles():
    assert model.slugify("Sourdough Coach: The App!") == "sourdough-coach-the-app"
    assert model.slugify("  ---  ") == "engagement"


def test_engagement_doc_roundtrips_through_render_and_parse():
    meta = {
        "schema": model.SCHEMA,
        "slug": "sourdough-coach",
        "title": "Sourdough Coach",
        "phase": "interview",
        "created_at": "2026-07-08T17:00:00Z",
        "updated_at": "2026-07-08T18:00:00Z",
        "phase_history": [
            "intake 2026-07-08T17:00:00Z",
            "interview 2026-07-08T18:00:00Z",
        ],
    }
    parsed = model.parse_engagement(model.render_engagement(meta))
    assert parsed == meta


def test_parse_rejects_non_engagement_docs():
    assert model.parse_engagement(None) is None
    assert model.parse_engagement("") is None
    assert model.parse_engagement("# just prose\n") is None
    assert model.parse_engagement("---\nschema: something.else\n---\n") is None


def test_render_sanitizes_newlines_in_scalars():
    # A newline-bearing title must not be able to corrupt the frontmatter
    # (e.g. by smuggling in a premature `---` terminator or extra keys).
    meta = {
        "schema": model.SCHEMA,
        "slug": "evil",
        "title": "Evil\n---\nphase: architecture",
        "phase": "intake",
        "created_at": "t0",
        "updated_at": "t0",
        "phase_history": ["intake t0"],
    }
    parsed = model.parse_engagement(model.render_engagement(meta))
    assert parsed is not None
    assert parsed["title"] == "Evil --- phase: architecture"
    assert parsed["phase"] == "intake"
    assert parsed["slug"] == "evil"
    assert parsed["schema"] == model.SCHEMA
    assert parsed["created_at"] == "t0"
    assert parsed["updated_at"] == "t0"
    assert parsed["phase_history"] == ["intake t0"]


def test_parse_tolerates_prose_body_and_blank_lines():
    text = model.render_engagement({
        "schema": model.SCHEMA, "slug": "x", "title": "X", "phase": "intake",
        "created_at": "t0", "updated_at": "t0",
        "phase_history": ["intake t0"],
    }) + "\nExtra prose the humans wrote.\n"
    parsed = model.parse_engagement(text)
    assert parsed is not None and parsed["slug"] == "x"
