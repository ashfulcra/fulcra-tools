"""Synthetic Python/browser permanent replay policy record parity."""
from datetime import datetime
import json
from pathlib import Path

import pytest

from fulcra_common.record_exclusions import _record

CASES = json.loads((Path(__file__).parent / "fixtures" / "v1_exclusion_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_permanent_exclusion_codec_preserves_target_identity_and_microseconds(case):
    value = case["input"]
    actual = _record(value["controlType"], value["targetDataType"], value["targetRecordId"],
        action_id=value["actionId"], requested_at=datetime.fromisoformat(value["requestedAt"].replace("Z", "+00:00")),
        reason=value["reason"], evidence=value["evidence"])
    assert actual == case["record"]
