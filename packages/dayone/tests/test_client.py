"""The public client explicitly retires annotation writes."""
import pytest
import httpx
from fulcra_dayone.client import DayOneFulcraClient


def test_old_annotation_api_has_clear_migration_error():
    client = DayOneFulcraClient(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected transport")))
    with pytest.raises(RuntimeError, match='JournalWriter'):
        client.ensure_journal_definition()
    with pytest.raises(RuntimeError, match='DayOneEntry'):
        client.run_import([])
