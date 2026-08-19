"""`linear-inbox` — read Ash's Linear board into a coord fold. Never writes.

THE RAIL IS IN THE CODE, NOT IN THE INTENT. coord-boss's order is that this
lane performs zero Linear writes of any kind until Ash approves a write plan in
his own words, and the reason it is non-negotiable is a near-miss: an earlier
cutover plan would have pushed ~503 creates into a 55-issue curated board.

A verb that merely *declines* to call a mutation is one hurried edit away from
calling one, so `ReadOnlyTransport` inspects the GraphQL document that is about
to be posted and refuses anything that is not a pure query. Same discipline as
coord-mesh's `refuse_share_all(argv)`: the guard runs on what will execute, not
on what the caller meant.

Read failure is UNKNOWN, never an empty board. `LinearClient.paginate` already
raises on a missing page or a stalled cursor; this module keeps that and adds a
`Result` whose state a caller cannot mistake for "Ash has no work".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .linear import GraphQLResponse, GraphQLTransport, LinearClient, LinearError

OK = "ok"
EMPTY = "empty"
UNKNOWN = "unknown"

#: A GraphQL document may declare operations; we permit exactly one kind.
_MUTATION = re.compile(r"(?<![A-Za-z0-9_])mutation(?![A-Za-z0-9_])", re.IGNORECASE)
_SUBSCRIPTION = re.compile(r"(?<![A-Za-z0-9_])subscription(?![A-Za-z0-9_])", re.IGNORECASE)


class WriteRefused(LinearError):
    """A write reached the read-only transport. Never caught internally."""


class ReadOnlyTransport:
    """Wraps a transport and refuses any document that is not a pure query.

    Checked on the payload actually about to be posted, so it also catches a
    mutation threaded in from a caller-built query string rather than from this
    module's own constants.
    """

    def __init__(self, inner: GraphQLTransport) -> None:
        self._inner = inner

    def post(self, payload: Mapping[str, Any]) -> GraphQLResponse:
        query = str(payload.get("query") or "")
        if _MUTATION.search(query) or _SUBSCRIPTION.search(query):
            raise WriteRefused(
                "read-only transport refused a non-query GraphQL document: the "
                "linear-inbox lane performs zero writes until Ash approves a "
                "write plan explicitly"
            )
        return self._inner.post(payload)


#: Deliberately narrower than the bridge's ISSUES_QUERY: this verb renders a
#: board, so it asks for what a reader needs and nothing else. Fewer fields is
#: also less of Ash's workspace pulled into a coord document.
INBOX_QUERY = (
    "query CoordInbox($team:ID!,$after:String)"
    "{issues(filter:{team:{id:{eq:$team}}},first:100,after:$after)"
    "{nodes{id identifier title url updatedAt "
    "state{name type} assignee{displayName} labels(first:10){nodes{name}}} "
    "pageInfo{hasNextPage endCursor}}}"
)


@dataclass(frozen=True, slots=True)
class InboxItem:
    identifier: str
    title: str
    state: str
    state_type: str
    assignee: str | None
    labels: tuple[str, ...]
    url: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class Result:
    state: str
    items: tuple[InboxItem, ...] = ()
    detail: str = ""

    @property
    def unknown(self) -> bool:
        """This read proves nothing. Callers must not render it as an empty board."""
        return self.state == UNKNOWN


class _Malformed(Exception):
    """A sub-object was present but the wrong shape. Never a default."""


def _name(node: Any, key: str = "name") -> str | None:
    if isinstance(node, Mapping):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_object(node: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """Absent is fine. Present-but-wrong-shape is NOT a default.

    THE RULE, arrived at the hard way over three review rounds. Every version of
    this file so far coerced a malformed sub-object into an empty one — labels
    became no labels, state became "unknown", assignee became unassigned — and
    each coercion renders a confident row from data we could not read. Absent
    and malformed are different facts and only one of them has a default.
    """
    value = node.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _Malformed(f"{key} is present but is not an object")
    return value


#: The fields this module ASKS FOR on each optional sub-object of an issue.
#: If the parent is PRESENT, every one of these must be readable or the row
#: degrades — "present but hollow" is malformed, not absent.
#:
#: This is a table rather than four hand-written checks on purpose. codex-coder
#: found the same absent-vs-malformed confusion at four sites across four review
#: rounds — nodes, labels, then state and assignee internals — because each fix
#: was written at the site that was named. A spec forces the question for the
#: next field somebody adds to INBOX_QUERY, and a test pins the two together so
#: they cannot drift apart silently.
_REQUIRED_SUBFIELDS: Mapping[str, tuple[str, ...]] = {
    "state": ("name", "type"),
    "assignee": ("displayName",),
}


#: Top-level scalars this module reads. REQUIRED ones must be present and
#: usable; OPTIONAL ones may be absent, but a present value must still be a
#: usable string. `identifier` is handled separately because it has a fallback,
#: and a fallback is exactly where absent and malformed get confused.
_REQUIRED_SCALARS: tuple[str, ...] = ("title",)
_OPTIONAL_SCALARS: tuple[str, ...] = ("url", "updatedAt")


def _usable_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _scalar(node: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    """THE INVARIANT, at scalar scope: every value is either ABSENT WITH A
    DEFAULT or VALIDATED WHOLE. There is no third state, and 'present but
    unusable' is never quietly turned into one of the first two."""
    if key not in node or node[key] is None:
        if required:
            raise _Malformed(f"{key} is absent and is required")
        return None
    usable = _usable_str(node[key])
    if usable is None:
        raise _Malformed(f"{key} is present but is not a usable string")
    return usable


def _identity(node: Mapping[str, Any]) -> str:
    """`identifier`, falling back to `id` ONLY when identifier is truly absent.

    codex-coder at 81858fd6: `node.get("identifier") or node.get("id")` fell
    back for a PRESENT-but-malformed identifier too — `identifier=[]` silently
    became the id, so a row we could not identify rendered as a row we could.
    A fallback is exactly where absent and malformed get confused, which is why
    this one is written out rather than expressed with `or`.
    """
    if "identifier" in node and node["identifier"] is not None:
        usable = _usable_str(node["identifier"])
        if usable is None:
            raise _Malformed("identifier is present but is not a usable string")
        return usable
    usable = _usable_str(node.get("id"))
    if usable is None:
        raise _Malformed("neither identifier nor id is a usable string")
    return usable


def _required_object(node: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """The sub-object, validated inside. None only when genuinely absent."""
    obj = _optional_object(node, key)
    if obj is None:
        return None
    for field in _REQUIRED_SUBFIELDS[key]:
        value = obj.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _Malformed(
                f"{key} is present but {key}.{field} is missing or unusable — "
                "a present-but-hollow object is malformed, not absent")
    return obj


def _labels(node: Mapping[str, Any]) -> tuple[str, ...]:
    """Label names, with CARDINALITY PRESERVED.

    codex-coder at 98ac86e: labels.nodes=[valid, null, {no name}] returned OK and
    emitted one label. That is the issue-level null-node defect they found at
    667befac, one level down, written by me WHILE fixing the issue-level one — I
    preserved cardinality where I had just been shown it mattered and dropped it
    two lines later where I had not.
    """
    root = _optional_object(node, "labels")
    if root is None:
        return ()
    raw = root.get("nodes")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _Malformed("labels.nodes is present but is not a list")
    names: list[str] = []
    for entry in raw:
        name = _name(entry)
        if name is None:
            raise _Malformed("a label node could not be named")
        names.append(name)
    return tuple(names)


def to_item(node: Mapping[str, Any]) -> InboxItem | None:
    """Normalize one issue node, or None when it cannot be read faithfully.

    None degrades the WHOLE read upstream — a board missing rows it never
    mentions is the same lie as an empty board, and a row missing labels it
    never mentions is the same lie one level down.
    """
    try:
        identifier = _identity(node)
        title = _scalar(node, "title", required=True)
        url = _scalar(node, "url", required=False)
        updated = _scalar(node, "updatedAt", required=False)
        state = _required_object(node, "state")
        assignee_obj = _required_object(node, "assignee")
        labels = _labels(node)
    except _Malformed:
        return None
    return InboxItem(
        identifier=identifier,
        title=title,
        # `state` is either absent — a genuine unknown — or validated whole.
        state=_name(state) if state else "unknown",
        state_type=(str(state["type"]).strip() if state else "unknown"),
        assignee=_name(assignee_obj, "displayName") if assignee_obj else None,
        labels=labels,
        url=url,
        updated_at=updated,
    )


def _paginate_preserving(client: LinearClient, team_id: str) -> list[Any]:
    """Walk the pages WITHOUT dropping a single node.

    We cannot reuse `LinearClient.paginate` here. It does
    ``nodes.extend(n for n in page["nodes"] if isinstance(n, Mapping))`` — a
    silent filter, which is harmless for a mirror that skips what it cannot
    project and fatal for a verb whose whole promise is that it never renders a
    partial board as a whole one. codex-coder reproduced it at 667befac: a page
    containing a null node came back state=empty, unknown=false, items=0. A
    clean empty board for a response we could not read, which is the exact
    failure this module was written to prevent — inherited from a library
    function I reused without auditing, having verified only the guard I wrote
    myself.

    So cardinality is preserved end to end and the identity check downstream is
    the only thing allowed to reject a row.
    """
    nodes: list[Any] = []
    cursor: str | None = None
    while True:
        data = client.execute("CoordInbox", INBOX_QUERY, {"team": team_id, "after": cursor})
        page = data.get("issues")
        if not isinstance(page, Mapping):
            raise LinearError("CoordInbox: missing page issues")
        raw = page.get("nodes")
        if not isinstance(raw, list):
            raise LinearError("CoordInbox: issues.nodes is not a list")
        nodes.extend(raw)                      # every node, nulls included
        # `x.get("pageInfo") or {}` only rescues a FALSY value. A truthy
        # non-Mapping — a list, a string — sails past it and AttributeErrors on
        # the next .get, which is a crash rather than an UNKNOWN and so escapes
        # the one contract this verb makes (codex-coder on 34a7220f). Every
        # shape is checked positively; nothing is assumed from truthiness.
        page_info = page.get("pageInfo")
        if page_info is None:
            # ABSENT pageInfo defaults to "no more pages". Present-but-hollow
            # does NOT — that distinction is the whole invariant, and coercing
            # absent into {} here would have made a missing pageInfo raise,
            # trading a silent-pass defect for a silent-fail one.
            return nodes
        if not isinstance(page_info, Mapping):
            raise LinearError("CoordInbox: issues.pageInfo is not an object")
        # THE INVARIANT INSIDE pageInfo (codex-coder on 41eaa87e). Fixing the
        # SHAPE of pageInfo in an earlier round is what made this scope
        # invisible to me: I had already "done" pageInfo, so I never asked the
        # same question of its contents. A falsy malformed hasNextPage — 0, "",
        # [] — read as "no more pages", stopping early and reporting a partial
        # board as complete; and a truthy non-string endCursor was coerced by
        # str(), sending fabricated pagination back to the platform.
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise LinearError(
                "CoordInbox: issues.pageInfo.hasNextPage is missing or not a "
                "boolean — pagination state is unreadable, and a partial board "
                "reported as complete is the failure this verb exists to avoid")
        if not has_next:
            return nodes
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor.strip():
            raise LinearError(
                "CoordInbox: issues.pageInfo.endCursor is missing or not a "
                "usable string while hasNextPage is true")
        next_cursor = next_cursor.strip()
        if next_cursor == cursor:
            raise LinearError("CoordInbox: pagination cursor did not advance")
        cursor = next_cursor


def fetch_inbox(client: LinearClient, team_id: str) -> Result:
    """One paginated read. Any failure is UNKNOWN, never an empty board."""
    try:
        nodes = _paginate_preserving(client, team_id)
    except LinearError as exc:
        return Result(UNKNOWN, detail=str(exc))
    items: list[InboxItem] = []
    for node in nodes:
        item = to_item(node) if isinstance(node, Mapping) else None
        if item is None:
            return Result(
                UNKNOWN,
                detail=(f"{len(nodes)} node(s) read but one could not be identified "
                        f"(null or unidentifiable row) — "
                        "the board is partial, and a partial board rendered as a "
                        "whole one is the failure this verb exists to avoid"),
            )
        items.append(item)
    ordered = tuple(sorted(items, key=lambda entry: entry.identifier))
    return Result(OK if ordered else EMPTY, items=ordered)


def render_fold(result: Result, *, team_id: str) -> str:
    """Deterministic text fold. Says UNKNOWN loudly; never prints a clean board
    for a read that failed."""
    if result.unknown:
        return (f"linear inbox {team_id}: UNKNOWN — could not read the board "
                f"({result.detail}). This is not an empty board.")
    if result.state == EMPTY:
        return f"linear inbox {team_id}: 0 issue(s) — read succeeded and found nothing."
    lines = [f"linear inbox {team_id}: {len(result.items)} issue(s)"]
    for item in result.items:
        who = item.assignee or "unassigned"
        tags = (" [" + ", ".join(item.labels) + "]") if item.labels else ""
        lines.append(f"  {item.identifier}  {item.state:<12} {who:<18} {item.title}{tags}")
    return "\n".join(lines)
