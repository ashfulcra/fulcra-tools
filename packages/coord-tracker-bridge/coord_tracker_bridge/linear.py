"""Linear GraphQL adapter with semantic operations, pagination, and bounded retry."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

import httpx

from .ledger import BridgeLedger
from .model import ManagedRecord, SourceIdentity
from .projection import Change, ChangeKind


LINEAR_API = "https://api.linear.app/graphql"
METADATA_PREFIX = "<!-- coord-tracker-bridge:source="
METADATA_SUFFIX = " -->"


class LinearError(RuntimeError):
    pass


class ResourceMissing(LinearError):
    pass


@dataclass(frozen=True, slots=True)
class GraphQLResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class GraphQLTransport(Protocol):
    def post(self, payload: Mapping[str, Any]) -> GraphQLResponse: ...


class HttpxGraphQLTransport:
    def __init__(self, api_key: str, *, url: str = LINEAR_API, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=url,
            timeout=timeout,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
        )

    def post(self, payload: Mapping[str, Any]) -> GraphQLResponse:
        response = self._client.post("", json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError(f"non-JSON Linear response ({response.status_code})") from exc
        return GraphQLResponse(response.status_code, body, response.headers)


class LinearClient:
    """Small GraphQL client whose retry policy is bounded and test-injectable."""

    def __init__(
        self,
        transport: GraphQLTransport,
        *,
        max_attempts: int = 3,
        base_backoff: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1 or base_backoff < 0:
            raise ValueError("invalid retry policy")
        self.transport = transport
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.sleeper = sleeper

    @staticmethod
    def _retryable(response: GraphQLResponse) -> bool:
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            return True
        errors = response.body.get("errors") if isinstance(response.body, Mapping) else None
        return bool(errors and any(
            str(error.get("extensions", {}).get("code", "")).upper()
            in {"RATELIMITED", "RATE_LIMITED", "INTERNAL_SERVER_ERROR"}
            for error in errors if isinstance(error, Mapping)
        ))

    def execute(
        self, operation: str, query: str, variables: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        # Variables can contain source text and secrets. They are deliberately
        # never included in exception strings or logs.
        payload = {"operationName": operation, "query": query, "variables": dict(variables or {})}
        for attempt in range(self.max_attempts):
            response = self.transport.post(payload)
            errors = response.body.get("errors") if isinstance(response.body, Mapping) else None
            if response.status_code < 400 and not errors:
                data = response.body.get("data")
                if not isinstance(data, Mapping):
                    raise LinearError(f"{operation}: missing data")
                return data
            if not self._retryable(response) or attempt + 1 >= self.max_attempts:
                raise LinearError(f"{operation}: Linear request failed after {attempt + 1} attempt(s)")
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after else self.base_backoff * (2**attempt)
            self.sleeper(min(delay, 30.0))
        raise AssertionError("unreachable")

    def paginate(
        self,
        operation: str,
        query: str,
        root: str,
        variables: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        nodes: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            page_variables = dict(variables or {})
            page_variables["after"] = cursor
            data = self.execute(operation, query, page_variables)
            page = data.get(root)
            if not isinstance(page, Mapping):
                raise LinearError(f"{operation}: missing page {root}")
            nodes.extend(node for node in page.get("nodes", []) if isinstance(node, Mapping))
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return nodes
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise LinearError(f"{operation}: invalid pagination cursor")
            cursor = str(next_cursor)


ISSUES_QUERY = """query Issues($team:ID!,$after:String){issues(filter:{team:{id:{eq:$team}}},first:100,after:$after){nodes{id title description priority dueDate state{id name type} project{id name}} pageInfo{hasNextPage endCursor}}}"""
LABELS_QUERY = """query Labels($team:ID!,$after:String){issueLabels(filter:{team:{id:{eq:$team}}},first:100,after:$after){nodes{id name} pageInfo{hasNextPage endCursor}}}"""
PROJECTS_QUERY = """query Projects($team:ID!,$after:String){projects(filter:{accessibleTeams:{id:{eq:$team}}},first:100,after:$after){nodes{id name} pageInfo{hasNextPage endCursor}}}"""
COMMENTS_QUERY = """query Comments($issue:ID!,$after:String){comments(filter:{issue:{id:{eq:$issue}}},first:100,after:$after){nodes{id body createdAt user{id}} pageInfo{hasNextPage endCursor}}}"""
ISSUE_LABELS_QUERY = """query IssueLabels($issue:ID!,$after:String){issue(id:$issue){labels(first:100,after:$after){nodes{id name} pageInfo{hasNextPage endCursor}}}}"""
EVENTS_QUERY = """query InboundEvents($team:ID!,$after:String){auditEntries(filter:{team:{id:{eq:$team}}},first:100,after:$after){nodes{id type createdAt actor{id} metadata} pageInfo{hasNextPage endCursor}}}"""
SCHEMA_QUERY = """query Schema($team:ID!){team(id:$team){id key states{nodes{id name type}}}}"""


def encode_source_metadata(source: SourceIdentity, fields: Mapping[str, Any] | None = None) -> str:
    value = {"source": source.to_dict(), "fields": dict(fields or {})}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def append_source_metadata(
    description: str, source: SourceIdentity, fields: Mapping[str, Any] | None = None
) -> str:
    clean = strip_source_metadata(description)
    marker = f"{METADATA_PREFIX}{encode_source_metadata(source, fields)}{METADATA_SUFFIX}"
    return f"{clean.rstrip()}\n\n{marker}".lstrip()


def parse_bridge_metadata(description: str) -> Mapping[str, Any] | None:
    start = description.rfind(METADATA_PREFIX)
    if start < 0:
        return None
    start += len(METADATA_PREFIX)
    end = description.find(METADATA_SUFFIX, start)
    if end < 0:
        return None
    encoded = description[start:end]
    try:
        encoded += "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded).decode())
        if "source" not in value:  # phase-1 probe compatibility
            value = {"source": value, "fields": {}}
        SourceIdentity.from_dict(value["source"])
        return value
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def parse_source_metadata(description: str) -> SourceIdentity | None:
    value = parse_bridge_metadata(description)
    return SourceIdentity.from_dict(value["source"]) if value else None


def strip_source_metadata(description: str) -> str:
    start = description.rfind(METADATA_PREFIX)
    if start < 0:
        return description
    end = description.find(METADATA_SUFFIX, start)
    if end < 0:
        return description
    return (description[:start] + description[end + len(METADATA_SUFFIX):]).rstrip()


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    labels: tuple[str, ...]
    projects: tuple[str, ...]


class LinearTrackerAdapter:
    provider = "linear"

    def __init__(self, client: LinearClient, team_id: str) -> None:
        self.client = client
        self.team_id = team_id
        self._metadata_by_source: dict[str, dict[str, Any]] = {}
        self._description_by_source: dict[str, str] = {}

    @property
    def tracker_id(self) -> str:
        return f"{self.provider}:{self.team_id}"

    def discover_schema(self) -> Mapping[str, Any]:
        return self.client.execute("Schema", SCHEMA_QUERY, {"team": self.team_id})["team"]

    def list_issues(self) -> list[Mapping[str, Any]]:
        return self.client.paginate("Issues", ISSUES_QUERY, "issues", {"team": self.team_id})

    def list_labels(self) -> list[Mapping[str, Any]]:
        return self.client.paginate("Labels", LABELS_QUERY, "issueLabels", {"team": self.team_id})

    def list_projects(self) -> list[Mapping[str, Any]]:
        return self.client.paginate("Projects", PROJECTS_QUERY, "projects", {"team": self.team_id})

    def list_comments(self, issue_id: str) -> list[Mapping[str, Any]]:
        return self.client.paginate("Comments", COMMENTS_QUERY, "comments", {"issue": issue_id})

    def list_issue_labels(self, issue_id: str) -> list[Mapping[str, Any]]:
        nodes: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self.client.execute(
                "IssueLabels", ISSUE_LABELS_QUERY, {"issue": issue_id, "after": cursor}
            )
            page = ((data.get("issue") or {}).get("labels") or {})
            nodes.extend(node for node in page.get("nodes", []) if isinstance(node, Mapping))
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return nodes
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor == cursor:
                raise LinearError("IssueLabels: invalid pagination cursor")
            cursor = str(next_cursor)

    def list_inbound_events(self) -> list[Mapping[str, Any]]:
        return self.client.paginate("InboundEvents", EVENTS_QUERY, "auditEntries", {"team": self.team_id})

    def list_managed_records(self, ledger: BridgeLedger) -> list[ManagedRecord]:
        ledger_by_provider = {entry.tracker_record_id: entry for entry in ledger}
        records: list[ManagedRecord] = []
        for issue in self.list_issues():
            provider_id = str(issue.get("id", ""))
            description = str(issue.get("description") or "")
            metadata = parse_bridge_metadata(description)
            source = SourceIdentity.from_dict(metadata["source"]) if metadata else None
            entry = ledger_by_provider.get(provider_id)
            if source is None and entry is not None:
                source = entry.source
            if source is None:
                continue
            internal = dict((metadata or {}).get("fields") or {})
            self._metadata_by_source[source.key] = internal
            self._description_by_source[source.key] = strip_source_metadata(description)
            labels = tuple(str(label.get("name")) for label in self.list_issue_labels(provider_id))
            state = issue.get("state") or {}
            records.append(ManagedRecord(
                provider_id=provider_id,
                source=source,
                capability=entry.capability if entry else "tasks",
                fields={
                    "title": issue.get("title"),
                    "description": strip_source_metadata(description),
                    "semantic_state": state.get("type") or state.get("name"),
                    "priority": issue.get("priority"),
                    "labels": labels,
                    "project": (issue.get("project") or {}).get("name"),
                    "due_at": issue.get("dueDate"),
                    "source_identity": source.to_dict(),
                    **internal,
                },
                closed=str(state.get("type", "")).lower() in {"completed", "canceled"},
            ))
        return records

    def resource_plan(self, labels: Iterable[str], projects: Iterable[str]) -> ResourcePlan:
        existing_labels = {str(value.get("name")) for value in self.list_labels()}
        existing_projects = {str(value.get("name")) for value in self.list_projects()}
        return ResourcePlan(
            tuple(sorted(set(labels) - existing_labels)),
            tuple(sorted(set(projects) - existing_projects)),
        )

    def apply_resources(self, plan: ResourcePlan) -> None:
        for label in plan.labels:
            self.client.execute(
                "CreateLabel",
                "mutation CreateLabel($input:IssueLabelCreateInput!){issueLabelCreate(input:$input){success}}",
                {"input": {"teamId": self.team_id, "name": label}},
            )
        for project in plan.projects:
            self.client.execute(
                "CreateProject",
                "mutation CreateProject($input:ProjectCreateInput!){projectCreate(input:$input){success}}",
                {"input": {"teamIds": [self.team_id], "name": project}},
            )

    def _state_id(self, semantic: str) -> str:
        schema = self.discover_schema()
        states = (schema.get("states") or {}).get("nodes", [])
        wanted = semantic.lower()
        for state in states:
            if not isinstance(state, Mapping):
                continue
            if str(state.get("type", "")).lower() == wanted or str(state.get("name", "")).lower() == wanted:
                return str(state["id"])
        raise ResourceMissing(f"Linear workflow has no semantic state {semantic!r}")

    def _resolved_fields(
        self, fields: Mapping[str, Any], source: SourceIdentity, *, ensure_metadata: bool = False
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        if "title" in fields:
            output["title"] = fields["title"]
        if "priority" in fields:
            output["priority"] = fields["priority"]
        if "labels" in fields:
            labels = {str(value.get("name")): str(value.get("id")) for value in self.list_labels()}
            missing = [label for label in fields["labels"] if label not in labels]
            if missing:
                raise ResourceMissing("run apply-resources before sync")
            output["labelIds"] = [labels[label] for label in fields["labels"]]
        if "project" in fields:
            project = fields["project"]
            if project:
                projects = {str(value.get("name")): str(value.get("id")) for value in self.list_projects()}
                if project not in projects:
                    raise ResourceMissing("run apply-resources before sync")
                output["projectId"] = projects[project]
            else:
                output["projectId"] = None
        if "due_at" in fields:
            output["dueDate"] = fields["due_at"]
        if "semantic_state" in fields:
            output["stateId"] = self._state_id(str(fields["semantic_state"]))

        internal_names = {
            "owner", "assignee", "origin", "workstream", "policy_version", "policy_hash"
        }
        internal = dict(self._metadata_by_source.get(source.key, {}))
        internal.update({key: fields[key] for key in internal_names if key in fields})
        metadata_changed = any(key in fields for key in internal_names)
        if "description" in fields or metadata_changed or ensure_metadata:
            visible = str(fields.get("description", self._description_by_source.get(source.key, "")) or "")
            output["description"] = append_source_metadata(visible, source, internal)
        return output

    def add_comment(self, issue_id: str, body: str) -> str:
        data = self.client.execute(
            "AddComment",
            "mutation AddComment($input:CommentCreateInput!){commentCreate(input:$input){comment{id}}}",
            {"input": {"issueId": issue_id, "body": body}},
        )
        return str(data["commentCreate"]["comment"]["id"])

    def set_due_date(self, issue_id: str, due_date: str | None) -> None:
        self.client.execute(
            "SetDueDate",
            "mutation SetDueDate($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}",
            {"id": issue_id, "input": {"dueDate": due_date}},
        )

    def apply_change(self, change: Change) -> str:
        if change.kind is ChangeKind.CLOSE:
            self.client.execute(
                "CloseIssue",
                "mutation CloseIssue($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}",
                {"id": change.provider_id, "input": {"stateId": self._state_id("completed")}},
            )
            return str(change.provider_id)
        payload = self._resolved_fields(
            change.fields, change.source, ensure_metadata=change.kind is ChangeKind.CREATE
        )
        if change.kind is ChangeKind.CREATE:
            payload["teamId"] = self.team_id
            data = self.client.execute(
                "CreateIssue",
                "mutation CreateIssue($input:IssueCreateInput!){issueCreate(input:$input){issue{id}}}",
                {"input": payload},
            )
            return str(data["issueCreate"]["issue"]["id"])
        self.client.execute(
            "UpdateIssue",
            "mutation UpdateIssue($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input){success}}",
            {"id": change.provider_id, "input": payload},
        )
        return str(change.provider_id)
