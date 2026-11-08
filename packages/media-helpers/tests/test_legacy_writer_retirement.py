"""Retired record seams refuse before any legacy work, even for no-op inputs."""
import pytest

from fulcra_common import BaseFulcraClient
from fulcra_common.ingest import IngestPipeline
from fulcra_media.fulcra import FulcraClient
from fulcra_media.state import State


class BombEvents:
    def __iter__(self):
        pytest.fail("input iteration before refusal")

    def __bool__(self):
        pytest.fail("input truth check before refusal")


@pytest.fixture
def blocked_client(monkeypatch):
    def bomb(*args, **kwargs):
        pytest.fail("legacy dependency before refusal")
    for name in ("get_token", "_client", "_lib", "_resolve_tag",
                 "fetch_existing_source_groups", "fetch_existing_source_ids",
                 "data_updates_summary", "records_visible"):
        monkeypatch.setattr(BaseFulcraClient, name, bomb)
    for name in ("ingest_batch", "ingest_typed"):
        monkeypatch.setattr(IngestPipeline, name, bomb)
    monkeypatch.setattr(FulcraClient, "ensure_tag", bomb)
    # Constructor remains available without authentication or transport effects.
    return FulcraClient(), bomb


@pytest.mark.parametrize("method", ["ingest_batch", "ingest_batch_typed", "run_import"])
@pytest.mark.parametrize("events", [[], [object()], BombEvents()], ids=["empty", "nonempty", "iterator-bomb"])
@pytest.mark.parametrize("check_only", [False, True])
def test_unbound_record_seams_refuse_first(blocked_client, method, events, check_only):
    client, bomb = blocked_client
    kwargs = {} if method != "run_import" else dict(
        check_only=check_only, claim=bomb, unclaim=bomb, updates_summary=bomb,
        sleep_fn=bomb,
    )
    with pytest.raises(RuntimeError, match="configured v1 standalone CLI.*captured Collect runtime"):
        getattr(client, method)(events, State(), **kwargs)


@pytest.mark.parametrize("cached", [False, True])
def test_unbound_definitions_refuse_before_cache_or_tags(blocked_client, cached):
    client, _ = blocked_client
    state = State(watched_definition_id="watched" if cached else None,
                  listened_definition_id="listened" if cached else None,
                  read_definition_id="read" if cached else None)
    with pytest.raises(RuntimeError, match="configured v1 standalone CLI.*captured Collect runtime"):
        client.ensure_definitions(state)
