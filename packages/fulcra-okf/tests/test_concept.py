from fulcra_okf.concept import Concept, concept_id_for


def test_concept_id_for_strips_md_and_uses_posix():
    assert concept_id_for("tables/users.md") == "tables/users"
    assert concept_id_for("tables\\users.md") == "tables/users"


def test_from_text_splits_known_and_extra_fields():
    text = (
        "---\n"
        "type: BigQuery Table\n"
        "title: Orders\n"
        "tags:\n- sales\n"
        "x_fulcra_weight: 1\n"
        "---\n"
        "Joined with [customers](/tables/customers.md).\n"
    )
    c = Concept.from_text(text, "tables/orders")
    assert c.id == "tables/orders"
    assert c.type == "BigQuery Table"
    assert c.title == "Orders"
    assert c.tags == ["sales"]
    assert c.extra == {"x_fulcra_weight": 1}
    assert c.description is None


def test_missing_type_becomes_empty_string():
    c = Concept.from_text("---\ntitle: NoType\n---\nbody\n", "x")
    assert c.type == ""


def test_tags_scalar_is_coerced_to_list():
    c = Concept.from_text("---\ntype: T\ntags: solo\n---\nb\n", "x")
    assert c.tags == ["solo"]


def test_links_resolves_relative_and_absolute_targets():
    text = (
        "---\ntype: T\n---\n"
        "See [a](/tables/a.md) and [b](b.md) and [ext](https://x.com).\n"
    )
    c = Concept.from_text(text, "tables/orders")
    assert c.links() == ["tables/a", "tables/b"]


def test_links_skips_anchors_and_mailto():
    """M4: #anchor and mailto: targets must be excluded from links()."""
    text = (
        "---\ntype: T\n---\n"
        "See [sec](#section), [mail](mailto:foo@example.com), and [real](other.md).\n"
    )
    c = Concept.from_text(text, "x")
    assert c.links() == ["other"]
