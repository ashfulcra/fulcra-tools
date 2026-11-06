"""Source and cloud reads must stop when a captured selection is revoked."""
import pytest

from fulcra_task_sync.engine import run_sync
from fulcra_task_sync.models import Collection, Task


@pytest.mark.parametrize("preview", [True, False])
def test_selection_revoked_after_collection_read_stops_before_tasks(preview):
    live = [True]
    reads = []
    class Provider:
        name, namespace = "synthetic", "synthetic-provider"
        def collections(self):
            reads.append("collections")
            live[0] = False
            return [Collection("synthetic-list", "Synthetic")]
        def tasks(self, selected):
            reads.append("tasks")
            return []
    class Vault:
        namespace = "synthetic-destination"
    result = run_sync(Provider(), {"synthetic-list"}, Vault(), lambda _:None,
        lambda *a: pytest.fail("revoked selection checkpointed"), dry_run=preview,
        still_selected=lambda: {"synthetic-list"} if live[0] else set())
    assert result.partial
    assert reads == ["collections"]


@pytest.mark.parametrize("preview", [True, False])
def test_revoked_generator_is_not_consumed_past_the_first_task(preview):
    live = [True]
    consumed = []
    class Provider:
        name, namespace = "synthetic", "synthetic-provider"
        def collections(self):
            return [Collection("synthetic-list", "Synthetic")]
        def tasks(self, selected):
            consumed.append("first")
            live[0] = False
            yield Task("one", "synthetic-list", "Synthetic", "", False)
            consumed.append("second")
            yield Task("two", "synthetic-list", "Synthetic", "", False)
    class Vault:
        namespace = "synthetic-destination"
    result = run_sync(Provider(), {"synthetic-list"}, Vault(), lambda _:None,
        lambda *a: pytest.fail("revoked selection checkpointed"), dry_run=preview,
        still_selected=lambda: {"synthetic-list"} if live[0] else set())
    assert result.partial
    assert consumed == ["first"]


@pytest.mark.parametrize('preview', [True, False])
def test_generator_revoked_at_exhaustion_cannot_start_another_source_read(preview):
    live, reads = [True], []
    class Provider:
        name, namespace = 'synthetic', 'synthetic-provider'
        def collections(self):
            yield Collection('synthetic-list', 'Synthetic')
            live[0] = False
        def tasks(self, selected):
            reads.append('tasks')
            return []
    class Vault:
        namespace = 'synthetic-destination'
    result = run_sync(Provider(), {'synthetic-list'}, Vault(), lambda _:None,
        lambda *a:pytest.fail('revoked selection checkpointed'), dry_run=preview,
        still_selected=lambda:{'synthetic-list'} if live[0] else set())
    assert result.partial and not reads
