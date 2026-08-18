"""The mesh safety rails, enforced HERE rather than by convention.

The plan's binding rules (v1.1 §SAFETY) are:

  - read-only against ALL existing datashares — cofounder shares are production;
  - test shares only to an operator-NAMED uid;
  - never revoke or delete any existing share or permission;
  - ``--share-all`` never — "refused in mesh code, not just convention".

"Not just convention" is the whole point: a rule that lives only in a plan doc
is one hurried agent away from being broken. Every function that could widen or
destroy access goes through a guard in this module, and the guards raise rather
than warn.
"""
import re
from typing import Iterable

#: A Fulcra user id is a UUID. Anything else is not a uid, and a mesh share is
#: never minted at a name, a role, a glob, or an empty string.
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


class SafetyViolation(Exception):
    """A mesh operation tried to cross a binding rail. Never caught internally."""


def require_named_uid(user_id: str) -> str:
    """The named-uid rail. Returns the uid, or raises.

    Rejects the empty string, wildcards, and anything not shaped like a UUID —
    so a bug that lets a name or a `*` reach share creation fails closed instead
    of granting to something unintended.
    """
    uid = (user_id or "").strip()
    if not uid:
        raise SafetyViolation("named-uid rail: no user id given")
    if not _UUID.match(uid):
        raise SafetyViolation(
            f"named-uid rail: {uid!r} is not a uid (mesh shares are minted at "
            "an operator-named UUID only — never a name, role, or wildcard)"
        )
    return uid


def refuse_share_all(argv: Iterable[str]) -> None:
    """The ``--share-all`` rail: refuse the flag anywhere in a share argv.

    Checked on the argv actually about to be executed, not on a caller's
    intent, so it also catches a flag threaded through from config or a
    caller-supplied extra-args list.
    """
    for a in argv:
        if str(a).strip() == "--share-all":
            raise SafetyViolation(
                "--share-all is refused by coord-mesh: the mesh mints scoped "
                "shares only (channel data type + reports prefix, named uid)"
            )


#: Verbs that destroy or narrow someone's existing access. The mesh never calls
#: them; revocation is operator-only. Listed explicitly so a future contributor
#: reaching for one gets a named refusal rather than a silent success.
FORBIDDEN_VERBS = ("delete", "leave", "revoke")


def refuse_destructive(verb: str) -> None:
    """The never-revoke rail."""
    v = (verb or "").strip().lower()
    if v in FORBIDDEN_VERBS:
        raise SafetyViolation(
            f"share verb {v!r} is refused by coord-mesh: revocation and leaving "
            "are operator-only; mesh tooling is read-only against existing shares"
        )
