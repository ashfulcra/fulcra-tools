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
    identifier = node.get("identifier") or node.get("id")
    title = node.get("title")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(title, str):
        return None
    try:
        state = _optional_object(node, "state") or {}
        assignee_obj = _optional_object(node, "assignee")
        labels = _labels(node)
    except _Malformed:
        return None
    url = node.get("url")
    updated = node.get("updatedAt")
    if url is not None and not isinstance(url, str):
        return None
    if updated is not None and not isinstance(updated, str):
        return None
    state_type = state.get("type")
    if state_type is not None and not isinstance(state_type, str):
        return None
    return InboxItem(
        identifier=identifier.strip(),
        title=title.strip(),
        state=_name(state) or "unknown",
        state_type=state_type or "unknown",
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
            page_info = {}
        if not isinstance(page_info, Mapping):
            raise LinearError("CoordInbox: issues.pageInfo is not an object")
        if not page_info.get("hasNextPage"):
            return nodes
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            raise LinearError("CoordInbox: invalid pagination cursor")
        cursor = str(next_cursor)


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
