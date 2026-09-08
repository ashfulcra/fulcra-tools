"""Synthetic API-v1 fixtures; no accounts, credentials, or network access."""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from fulcra_todoist.provider import TodoistError, TodoistProvider

NOW = datetime(2026, 2, 10, tzinfo=timezone.utc)


def task(**overrides):
    return (
        dict(
            id="task-a",
            project_id="project-a",
            content="Synthetic task",
            description="Example notes",
            checked=False,
            is_deleted=False,
            due=None,
            completed_at=None,
            updated_at="2026-02-01T12:00:00Z",
            completed_count=0,
            **overrides,
        )
        if not overrides
        else {**task(), **overrides}
    )


class API:
    def __init__(self):
        self.requests = []
        self.active = [task()]
        self.history = []
        self.account = {"id": "synthetic-account"}
        self.override = None
        self.commands = []
        self.command_status = "ok"
        self.lose_response = False

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.todoist.com"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        path = request.url.path
        if path == "/api/v1/user":
            return httpx.Response(200, json=self.account)
        if path == "/api/v1/projects":
            return httpx.Response(
                200,
                json={
                    "results": [
                        dict(
                            id="project-a",
                            name="Example",
                            is_deleted=False,
                            is_archived=False,
                            is_frozen=False,
                            role="admin",
                        )
                    ],
                    "next_cursor": None,
                },
            )
        if path == "/api/v1/tasks":
            return httpx.Response(200, json={"results": self.active, "next_cursor": None})
        if path == "/api/v1/tasks/completed/by_completion_date":
            return httpx.Response(200, json={"items": self.history})
        if path.startswith("/api/v1/tasks/"):
            found = next((t for t in self.active if t["id"] == path.rsplit("/", 1)[1]), None)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path == "/api/v1/sync":
            form = parse_qs(request.content.decode())
            command = json.loads(form["commands"][0])[0]
            self.commands.append(command)
            assert command["type"] == "item_close"
            if self.lose_response:
                raise httpx.ReadTimeout("synthetic-token secret body")
            if self.command_status == "ok":
                found = self.active.pop(0)
                self.history = [{**found, "completed_at": "2026-02-09T12:00:00Z"}]
            return httpx.Response(200, json={"sync_status": {command["uuid"]: self.command_status}})
        pytest.fail(f"Unexpected API endpoint: {path}")

    def provider(self, **kwargs):
        return TodoistProvider(
            "synthetic-token", transport=httpx.MockTransport(self.handle), now=lambda: NOW, **kwargs
        )


def test_account_namespace_is_verified_and_not_token_derived():
    api = API()
    with api.provider() as provider:
        namespace = provider.namespace
        assert namespace and namespace != "synthetic-token"
        assert api.requests[0].url.path == "/api/v1/user"
        assert provider.namespace == namespace
        assert len(api.requests) == 1
    api.account = {"id": "another-synthetic-account"}
    assert api.provider().namespace != namespace


@pytest.mark.parametrize("account", [{}, {"id": None}, {"id": ""}, {"id": True}])
def test_invalid_account_identity_fails_closed(account):
    api = API()
    api.account = account
    with pytest.raises(TodoistError):
        api.provider().namespace


def test_404_is_unavailable_not_completed_even_with_stale_cache():
    api = API()
    provider = api.provider()
    provider.tasks({"project-a"})
    api.active = []
    assert provider.get_task("task-a") is None


def test_explicit_history_imports_completion_but_reopened_active_wins():
    api = API()
    api.history = [
        task(completed_at="2026-02-02T12:00:00Z"),
        task(id="task-b", completed_at="2026-02-03T12:00:00Z"),
    ]
    found = {t.id: t for t in api.provider().tasks({"project-a"})}
    assert not found["task-a"].completed
    assert found["task-b"].completed
    assert found["task-b"].completed_at == "2026-02-03T12:00:00Z"


def test_selected_project_filtering_and_empty_selection():
    api = API()
    api.active += [task(id="unselected", project_id="project-b")]
    api.history = [
        task(id="old-unselected", project_id="project-b", completed_at="2026-02-02T12:00:00Z")
    ]
    provider = api.provider()
    assert [t.id for t in provider.tasks({"project-a"})] == ["task-a"]
    for request in api.requests:
        if request.url.path in ("/api/v1/tasks", "/api/v1/tasks/completed/by_completion_date"):
            assert request.url.params["project_id"] == "project-a"
    api.requests.clear()
    assert provider.tasks(set()) == []
    assert not api.requests


def test_all_pages_are_read_and_history_window_stays_fixed():
    api = API()

    def pages(request):
        if request.url.path.endswith("by_completion_date"):
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"items": [], "next_cursor": "opaque.page"})
            return httpx.Response(
                200, json={"items": [task(id="task-b", completed_at="2026-02-02T12:00:00Z")]}
            )

    api.override = pages
    assert len(api.provider().tasks({"project-a"})) == 2
    requests = [r for r in api.requests if r.url.path.endswith("by_completion_date")]
    assert requests[0].url.params["since"] == "2026-01-11T00:00:00Z"
    assert requests[0].url.params["until"] == requests[1].url.params["until"]


@pytest.mark.parametrize("mode", ["loop", "error", "malformed", "cap"])
def test_incomplete_pagination_fails_before_returning_or_authorizing_writes(mode):
    api = API()

    def incomplete(request):
        if request.url.path == "/api/v1/tasks":
            if "cursor" not in request.url.params:
                return httpx.Response(200, json={"results": [task()], "next_cursor": "opaque.page"})
            if mode == "error":
                return httpx.Response(503, text="synthetic-token")
            if mode == "malformed":
                return httpx.Response(200, json={"next_cursor": None})
            return httpx.Response(200, json={"results": [], "next_cursor": "opaque.page"})

    api.override = incomplete
    provider = api.provider(max_pages=1 if mode == "cap" else 3)
    with pytest.raises(TodoistError):
        provider.tasks({"project-a"})
    with pytest.raises(TodoistError):
        provider.complete("task-a", "revision", "project-a")
    assert not api.commands


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_errors_are_sanitized_and_redirects_are_not_followed(status):
    api = API()
    api.override = lambda request: httpx.Response(
        status,
        text="synthetic-token private text",
        headers={"location": "https://untrusted.invalid/token"},
    )
    with pytest.raises(TodoistError) as exc:
        api.provider().namespace
    assert "synthetic-token" not in str(exc.value)
    assert "private text" not in str(exc.value)
    assert len(api.requests) == 1


def test_recurring_completion_is_refused_without_post():
    api = API()
    api.active = [task(due={"date": "2026-02-10", "is_recurring": True})]
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]
    assert current.recurring
    with pytest.raises(TodoistError, match="recurring.*read.only"):
        provider.complete(current.id, current.revision, current.collection_id)
    assert not api.commands


def test_revision_change_and_project_move_refuse_completion():
    api = API()
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]
    api.active = [task(updated_at="2026-02-04T12:00:00Z")]
    assert provider.get_task(current.id).revision != current.revision
    with pytest.raises(TodoistError, match="changed"):
        provider.complete(current.id, current.revision, current.collection_id)
    api.active = [task(project_id="project-b")]
    with pytest.raises(TodoistError):
        provider.complete(current.id, current.revision, current.collection_id)
    assert not api.commands


def test_completion_requires_successful_selected_snapshot():
    api = API()
    provider = api.provider()
    current = provider.get_task("task-a")
    with pytest.raises(TodoistError):
        provider.complete(current.id, current.revision, "project-a")
    assert not api.commands


def test_deselection_revokes_completion_authority():
    api = API()
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]
    provider.tasks(set())
    with pytest.raises(TodoistError):
        provider.complete(current.id, current.revision, current.collection_id)
    assert not api.commands


def test_uncertain_retry_reuses_command_uuid_across_provider_restart():
    api = API()
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]
    api.lose_response = True
    with pytest.raises(TodoistError):
        provider.complete(current.id, current.revision, current.collection_id)
    api.lose_response = False
    provider.close()
    provider = api.provider()
    provider.tasks({"project-a"})
    result = provider.complete(current.id, current.revision, current.collection_id)
    assert result.completed
    assert len(api.commands) == 2
    assert api.commands[0] == api.commands[1]
    assert api.commands[0]["args"] == {"id": "task-a"}


@pytest.mark.parametrize(
    "status", [{"error_code": 403, "error": "synthetic-token"}, None, "failed"]
)
def test_http_success_with_command_error_never_claims_completion(status):
    api = API()
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]
    api.command_status = status
    with pytest.raises(TodoistError) as exc:
        provider.complete(current.id, current.revision, current.collection_id)
    assert "synthetic-token" not in str(exc.value)
    assert not provider.get_task(current.id).completed


def test_discovery_is_read_only_and_follows_project_pages():
    api = API()
    provider = api.provider()
    assert [(c.id, c.name, c.writable) for c in provider.collections()] == [
        ("project-a", "Example", True)
    ]
    assert all(r.method == "GET" for r in api.requests)
    assert not any(r.url.path == "/api/v1/tasks" for r in api.requests)


def test_prior_completion_cannot_close_a_reopened_then_missing_task():
    api = API()
    api.active = [task(updated_at="2026-02-05T12:00:00Z", completed_count=1)]
    api.history = [task(completed_at="2026-02-02T12:00:00Z")]
    provider = api.provider()
    provider.tasks({"project-a"})
    api.active = []
    assert provider.get_task("task-a") is None
    assert provider.tasks({"project-a"}) == []


def test_confirmed_command_then_lost_response_does_not_send_second_command():
    api = API()
    provider = api.provider()
    current = provider.tasks({"project-a"})[0]

    def lose_committed(request):
        if request.url.path == "/api/v1/sync":
            api.active = []
            api.history = [task(completed_at="2026-02-09T12:00:00Z")]
            raise httpx.ReadTimeout("synthetic-token")

    api.override = lose_committed
    with pytest.raises(TodoistError):
        provider.complete(current.id, current.revision, current.collection_id)
    api.override = None
    provider.close()
    provider = api.provider()
    provider.tasks({"project-a"})
    result = provider.complete(current.id, current.revision, current.collection_id)
    assert result.completed
    assert len([r for r in api.requests if r.method == "POST"]) == 1


def test_request_budget_exhaustion_revokes_snapshot_authority():
    api = API()
    provider = api.provider(max_requests=2)
    with pytest.raises(TodoistError, match="budget"):
        provider.tasks({"project-a"})
    assert len(api.requests) == 2
    assert not api.commands


def test_oversized_response_fails_closed():
    api = API()
    api.override = lambda request: httpx.Response(200, content=b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(TodoistError, match="limit"):
        api.provider().namespace


def test_malicious_source_id_never_changes_request_target():
    api = API()
    provider = api.provider()
    with pytest.raises(TodoistError):
        provider.get_task("../user")
    assert all(r.url.path == "/api/v1/user" for r in api.requests)


@pytest.mark.parametrize(
    "row",
    [
        task(due={"date": "2026-02-01"}),
        task(checked="false"),
        task(is_deleted=None),
        task(content=None),
    ],
)
def test_malformed_source_task_aborts_snapshot(row):
    api = API()
    api.active = [row]
    with pytest.raises(TodoistError):
        api.provider().tasks({"project-a"})


def test_completion_timestamp_is_required_even_when_history_checked_false():
    api = API()
    api.history = [task(id="task-b")]
    with pytest.raises(TodoistError):
        api.provider().tasks({"project-a"})


def test_deleted_tombstone_is_never_completed():
    api = API()
    api.active = [task(is_deleted=True)]
    assert api.provider().tasks({"project-a"}) == []


def test_revision_changes_with_source_completion_counter():
    api = API()
    provider = api.provider()
    current = provider.get_task("task-a")
    api.active = [task(completed_count=1)]
    assert provider.get_task("task-a").revision != current.revision


def test_discovery_paginates_projects_and_marks_viewers_read_only():
    api = API()

    def pages(request):
        if request.url.path == "/api/v1/projects":
            if "cursor" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": "project-a", "name": "Example", "role": "admin"}],
                        "next_cursor": "opaque.page",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "project-b", "name": "Shared", "role": "viewer"}],
                    "next_cursor": None,
                },
            )

    api.override = pages
    assert [(c.id, c.writable) for c in api.provider().collections()] == [
        ("project-a", True),
        ("project-b", False),
    ]


def test_deleted_tombstone_overrides_old_history_in_snapshot_and_point_read():
    api = API()
    api.history = [task(completed_at="2026-02-02T12:00:00Z")]
    provider = api.provider()
    provider.tasks({"project-a"})
    api.active = [task(is_deleted=True)]
    assert provider.get_task("task-a") is None
    assert provider.tasks({"project-a"}) == []


def test_active_endpoint_returning_checked_task_is_inconsistent_and_fails_closed():
    api = API()
    api.active = [task(checked=True, completed_at="2026-02-02T12:00:00Z")]
    with pytest.raises(TodoistError):
        api.provider().tasks({"project-a"})


class State:
    def __init__(self):
        self.values = {}
        self.reads = []
        self.writes = []

    def load(self, key):
        self.reads.append(key)
        return self.values.get(key)

    def save(self, key, value):
        assert len(key.encode()) <= 256
        assert len(json.dumps(value).encode()) <= 65536
        self.writes.append((key, value))
        self.values[key] = value

    def provider(self, api, **kwargs):
        return api.provider(load_state=self.load, save_state=self.save, **kwargs)


def test_persisted_active_baseline_rejects_old_completion_after_restart():
    api = API()
    state = State()
    api.active = [task(updated_at="2026-02-05T12:00:00Z")]
    with state.provider(api) as provider:
        assert provider.tasks({"project-a"})[0].completed is False
    assert len(state.writes) == 1
    api.active = []
    api.history = [task(completed_at="2026-02-02T12:00:00Z")]
    with state.provider(api) as provider:
        assert provider.tasks({"project-a"}) == []
        assert provider.get_task("task-a") is None
    assert len(state.writes) == 1


def test_new_completion_after_saved_active_baseline_is_imported():
    api = API()
    state = State()
    with state.provider(api) as provider:
        provider.tasks({"project-a"})
    api.active = []
    api.history = [task(completed_at="2026-02-09T12:00:00Z")]
    with state.provider(api) as provider:
        assert provider.tasks({"project-a"})[0].completed is True


def test_partial_snapshot_never_commits_observed_active_baseline():
    api = API()
    state = State()

    def fail_second_project(request):
        if request.url.path == "/api/v1/tasks" and request.url.params["project_id"] == "project-b":
            return httpx.Response(503)

    api.override = fail_second_project
    with state.provider(api) as provider:
        with pytest.raises(TodoistError):
            provider.tasks({"project-a", "project-b"})
    assert state.writes == []
    assert state.values == {}


def test_dry_run_reads_saved_baseline_but_never_writes_state_or_source():
    api = API()
    state = State()
    with state.provider(api, dry_run=True) as provider:
        current = provider.tasks({"project-a"})[0]
        with pytest.raises(TodoistError, match="preview"):
            provider.complete(current.id, current.revision, current.collection_id)
    assert state.reads
    assert not state.writes
    assert not api.commands


def test_state_keys_isolate_verified_accounts_with_same_task_id():
    api = API()
    state = State()
    api.active = [task(updated_at="2026-02-05T12:00:00Z")]
    with state.provider(api) as provider:
        provider.tasks({"project-a"})
    first_key = next(iter(state.values))
    assert first_key.startswith("todoist-observation:v1:")
    assert "synthetic-account" not in first_key and "task-a" not in first_key
    api.account = {"id": "another-synthetic-account"}
    api.active = []
    api.history = [task(completed_at="2026-02-02T12:00:00Z")]
    with state.provider(api) as provider:
        assert provider.tasks({"project-a"})[0].completed
    assert state.reads[-1] != first_key


@pytest.mark.parametrize(
    "invalid",
    [
        False,
        [],
        {},
        {"schema": "wrong", "active_updated_at": "2026-02-05T12:00:00Z"},
        {"schema": "todoist-observation/v1", "active_updated_at": "invalid"},
        {"schema": "todoist-observation/v1", "active_updated_at": "2026-02-05T12:00:00"},
        {
            "schema": "todoist-observation/v1",
            "active_updated_at": "2026-02-05T12:00:00Z",
            "unexpected": True,
        },
    ],
)
def test_malformed_stored_observation_fails_closed(invalid):
    api = API()
    writes = []
    with api.provider(
        load_state=lambda key: invalid, save_state=lambda *args: writes.append(args)
    ) as provider:
        with pytest.raises(TodoistError, match="observation"):
            provider.tasks({"project-a"})
    assert not writes
    assert not api.commands


def test_state_callbacks_are_not_called_before_identity_verification():
    api = API()
    api.account = {"id": None}
    state = State()
    with state.provider(api) as provider:
        with pytest.raises(TodoistError):
            provider.tasks({"project-a"})
    assert not state.reads and not state.writes


def test_state_failure_is_sanitized_and_prevents_completion_authority():
    api = API()

    def fail_save(key, value):
        raise RuntimeError("synthetic-token private error")

    with api.provider(load_state=lambda key: None, save_state=fail_save) as provider:
        with pytest.raises(TodoistError, match="observation") as exc:
            provider.tasks({"project-a"})
        assert "synthetic-token" not in str(exc.value)
        with pytest.raises(TodoistError):
            provider.complete("task-a", "revision", "project-a")
    assert not api.commands
