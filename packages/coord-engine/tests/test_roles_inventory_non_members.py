"""A documented-optional courtesy file must not poison the roles section.

Found 2026-08-30, after fulcra reconcile had been REFUSING PUBLICATION for
weeks. Publication is all-or-nothing, so one incomplete section keeps every
fold stale for every agent, which is why obligations reads were returning
UNKNOWN fleet-wide.

The mechanism: `_tree_section` reads every file under `roles/` and returns
UNKNOWN for the WHOLE section the moment one file fails
`canonical_inventory_document`. That fail-closed rule is right for a corrupt
Role document — malformed source must never publish as a complete view. But
it also fired on two files that were never inventory in the first place:

  roles/index.md                                   type: RolesIndex
  roles/<role>/escalations/<date>.md               type: RoleEscalation

The index is the sharp one: our own published `fulcra-agent-roles` skill tells
users a `roles/index.md` is "optional human courtesy", so anyone following our
documentation permanently breaks their own reconcile publication. The
escalation is a legacy type name — the current writer emits "Escalation"
(cli.py) while an older engine emitted "RoleEscalation", and the document is a
genuine escalation record sitting at the correct path.

So the two get DIFFERENT treatment, and the difference is the point:
- a legacy-typed escalation is a real inventory MEMBER (accepted, included);
- a courtesy index is NOT a member and is SKIPPED (excluded from records,
  and explicitly not a reason to fail the section).

Everything else still fails closed. The tests that matter most here are the
negative controls proving that: an unknown file type under roles/, and a
top-level document that is not a Role, must still make the section UNKNOWN.
"""
from __future__ import annotations

from coord_engine import generation


ROLES = "team/fulcra/roles/"


def _member(path, doc):
    return generation.canonical_inventory_document("roles", ROLES + path, doc)


def _ignorable(path, doc):
    return generation.ignorable_inventory_file("roles", ROLES + path, doc)


# --- the two files that were blocking publication -------------------------

def test_the_courtesy_index_is_not_a_member_but_is_ignorable():
    """`roles/index.md` is documented as optional in our own skill. It is not
    inventory, so it must not be a member — and must not fail the section."""
    doc = {"type": "RolesIndex", "title": "Fulcra coord roles"}
    assert _member("index.md", doc) is False
    assert _ignorable("index.md", doc) is True


def test_a_legacy_typed_escalation_is_accepted_as_a_member():
    """Same path shape, older type name, genuine escalation record."""
    doc = {"type": "RoleEscalation", "role": "coord-maintainer"}
    assert _member("coord-maintainer/escalations/2026-07-24.md", doc) is True


def test_the_current_escalation_type_still_works():
    """Positive control: the type the current writer emits is unaffected."""
    doc = {"type": "Escalation", "role": "coord-maintainer"}
    assert _member("coord-maintainer/escalations/2026-07-24.md", doc) is True


# --- fail-closed must survive: the negative controls -----------------------

def test_an_unknown_file_under_roles_still_fails_closed():
    """NOT ignorable: an unrecognised document is exactly the corruption the
    all-or-nothing rule exists to catch, and must still poison the section."""
    doc = {"type": "Task", "title": "not a role"}
    assert _member("something.md", doc) is False
    assert _ignorable("something.md", doc) is False


def test_an_index_named_file_that_is_real_inventory_is_not_ignorable():
    """Inventory always wins over the skip. A document at `roles/index.md`
    that classifies as a Role definition is a MEMBER — were a role ever
    actually named "index", it must be folded, not silently dropped."""
    doc = {"type": "Role", "title": "sneaky"}
    assert _member("index.md", doc) is True
    assert _ignorable("index.md", doc) is False


def test_an_untyped_courtesy_index_is_ignorable():
    """The skip is keyed to the PATH, not the declared type.

    The first version of this predicate also required `type: RolesIndex`,
    which left the bug live for exactly the people it was written for: the
    `fulcra-agent-roles` skill tells a human to write this index by hand and
    never tells them to type it. An untyped index froze their whole fleet.
    """
    assert _ignorable("index.md", {"title": "Fulcra coord roles"}) is True
    assert _ignorable("index.md", {}) is True
    assert _ignorable("index.md", {"type": "Index"}) is True
    assert _member("index.md", {"title": "Fulcra coord roles"}) is False


def test_a_rolesindex_type_elsewhere_is_not_ignorable():
    """The skip is keyed to the exact top-level path too — a RolesIndex buried
    inside a role directory is misfiled, not courtesy."""
    doc = {"type": "RolesIndex", "title": "misfiled"}
    assert _ignorable("coord-boss/index.md", doc) is False
    assert _ignorable("coord-boss/leases/index.md", doc) is False


def test_a_lease_without_an_agent_still_fails_closed():
    """Unchanged rule, pinned so the new skip cannot widen it."""
    assert _member("coord-boss/leases/x.md", {"type": "Lease"}) is False
    assert _ignorable("coord-boss/leases/x.md", {"type": "Lease"}) is False


def test_other_sections_have_no_ignorable_files():
    """The courtesy carve-out is roles-only; presence stays strict."""
    doc = {"type": "RolesIndex"}
    assert generation.ignorable_inventory_file(
        "presence", "team/fulcra/presence/index.md", doc) is False


# --- end to end: the section itself, through the tree reader ---------------

class _Store:
    """Minimal transport over an in-memory {path: text} map."""

    def __init__(self, files):
        self.files = files

    def list_dir(self, prefix):
        seen, out = set(), []
        for path in self.files:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            head = rest.split("/")[0]
            is_dir = "/" in rest
            name = head + "/" if is_dir else head
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "is_dir": is_dir})
        return out

    def read(self, path):
        return self.files.get(path)


def _doc(doc_type, **kw):
    lines = [f"{k}: {v}" for k, v in {"type": doc_type, **kw}.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n# doc\n"


def _roles_section(files):
    from coord_engine import reconcile
    return reconcile._tree_section(
        _Store(files), "team/fulcra/roles/", section="roles",
        deadline=reconcile.Deadline(1e6))


def test_the_real_store_shape_now_completes():
    """The exact two files that were blocking team/fulcra, plus a real role
    and lease. Before the fix this returned UNKNOWN and publication refused."""
    files = {
        "team/fulcra/roles/index.md": _doc("RolesIndex", title="courtesy"),
        "team/fulcra/roles/coord-boss.md": _doc("Role", title="coord-boss"),
        "team/fulcra/roles/coord-boss/leases/a.md": _doc("Lease", agent="x"),
        "team/fulcra/roles/coord-maintainer/escalations/2026-07-24.md":
            _doc("RoleEscalation", role="coord-maintainer"),
    }
    state, value = _roles_section(files)
    assert state == "DATA"
    paths = [r["path"] for r in value["records"]]
    # the courtesy index is skipped, never sealed into the generation
    assert "team/fulcra/roles/index.md" not in paths
    # the legacy escalation IS a member
    assert ("team/fulcra/roles/coord-maintainer/escalations/2026-07-24.md"
            in paths)
    assert len(paths) == 3


def test_a_corrupt_role_document_still_makes_the_section_unknown():
    """The negative control that matters: the fix must not have turned the
    all-or-nothing guard into a skip-everything-unrecognised."""
    files = {
        "team/fulcra/roles/coord-boss.md": _doc("Role", title="coord-boss"),
        "team/fulcra/roles/garbage.md": _doc("Task", title="not a role"),
    }
    state, value = _roles_section(files)
    assert state == "UNKNOWN"
    assert value["records"] == []


# --- the prefix table must not be edit-sensitive ---------------------------

def test_the_section_marker_tolerates_a_prefix_without_a_trailing_slash():
    """Every classifier splits on this marker and reads what follows as the
    relative path. A prefix that lost its trailing slash would not fail — it
    would shift every relative path by the missing characters and quietly
    reclassify the section, so the marker normalises instead of trusting the
    table's punctuation."""
    assert generation._section_marker("roles") == "/roles/"
    original = dict(generation.INVENTORY_PREFIXES)
    try:
        generation.INVENTORY_PREFIXES["roles"] = "roles"
        assert generation._section_marker("roles") == "/roles/"
        assert _member("coord-boss.md", {"type": "Role"}) is True
        assert _ignorable("index.md", {"type": "RolesIndex"}) is True
    finally:
        generation.INVENTORY_PREFIXES.clear()
        generation.INVENTORY_PREFIXES.update(original)


def test_an_unknown_section_has_no_marker_and_classifies_nothing():
    assert generation._section_marker("no-such-section") == ""
    assert generation.canonical_inventory_document(
        "no-such-section", "team/fulcra/roles/coord-boss.md", {"type": "Role"},
    ) is False
