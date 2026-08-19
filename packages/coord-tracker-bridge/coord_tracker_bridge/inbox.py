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


def _name(node: Any, key: str = "name") -> str | None:
    if isinstance(node, Mapping):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def to_item(node: Mapping[str, Any]) -> InboxItem | None:
    """Normalize one issue node, or None when it cannot be identified.

    An unidentifiable row is not silently dropped by this function's caller —
    it degrades the whole read, because a board missing rows it never mentions
    is the same lie as an empty board.
    """
    identifier = node.get("identifier") or node.get("id")
    title = node.get("title")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(title, str):
        return None
    state = node.get("state") if isinstance(node.get("state"), Mapping) else {}
    labels_root = node.get("labels") if isinstance(node.get("labels"), Mapping) else {}
    label_nodes = labels_root.get("nodes") if isinstance(labels_root, Mapping) else []
    labels = tuple(
        name for name in (_name(entry) for entry in (label_nodes or []))
        if name is not None
    )
    return InboxItem(
        identifier=identifier.strip(),
        title=title.strip(),
        state=_name(state) or "unknown",
        state_type=str(state.get("type") or "unknown"),
        assignee=_name(node.get("assignee"), "displayName"),
        labels=labels,
        url=node.get("url") if isinstance(node.get("url"), str) else None,
        updated_at=node.get("updatedAt") if isinstance(node.get("updatedAt"), str) else None,
    )


def fetch_inbox(client: LinearClient, team_id: str) -> Result:
    """One paginated read. Any failure is UNKNOWN, never an empty board."""
    try:
        nodes = client.paginate("CoordInbox", INBOX_QUERY, "issues", {"team": team_id})
    except LinearError as exc:
        return Result(UNKNOWN, detail=str(exc))
    items: list[InboxItem] = []
    for node in nodes:
        item = to_item(node) if isinstance(node, Mapping) else None
        if item is None:
            return Result(
                UNKNOWN,
                detail=(f"{len(nodes)} node(s) read but one could not be identified — "
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
