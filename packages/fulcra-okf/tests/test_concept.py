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


# --- Finding 1: links() must strip URL fragments and query strings ---

def test_links_strips_fragment_from_absolute_link():
    """Absolute link /a.md#schema should resolve to concept id 'a', not 'a#schema'."""
    text = "---\ntype: T\n---\nSee [schema](/a.md#schema).\n"
    c = Concept.from_text(text, "tables/orders")
    assert c.links() == ["a"]


def test_links_strips_fragment_from_relative_link_in_subdir():
    """Relative link b.md#x from a subdir concept should resolve correctly."""
    text = "---\ntype: T\n---\nSee [b](b.md#x).\n"
    c = Concept.from_text(text, "subdir/orders")
    assert c.links() == ["subdir/b"]


def test_links_strips_query_string():
    """a.md?v=1 should resolve to concept id 'a'."""
    text = "---\ntype: T\n---\nSee [versioned](a.md?v=1).\n"
    c = Concept.from_text(text, "x")
    assert c.links() == ["a"]


def test_links_strips_query_and_fragment():
    """a.md?v=1#x should resolve to concept id 'a'."""
    text = "---\ntype: T\n---\nSee [both](a.md?v=1#x).\n"
    c = Concept.from_text(text, "x")
    assert c.links() == ["a"]


def test_links_pure_anchor_still_skipped():
    """A pure #section link (same-concept anchor) must still be excluded from links()."""
    text = "---\ntype: T\n---\nSee [top](#top).\n"
    c = Concept.from_text(text, "x")
    assert c.links() == []
