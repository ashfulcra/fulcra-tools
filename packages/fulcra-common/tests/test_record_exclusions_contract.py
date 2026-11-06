"""The browser and Python replay policy share one packaged immutable contract."""
import json
from pathlib import Path

from fulcra_common import record_exclusions


def test_canonical_exclusion_contract_retains_the_existing_creation_identity():
    path = Path(record_exclusions.__file__).with_name("record_exclusions_contract.json")
    contract = json.loads(path.read_text())
    spec = record_exclusions.EXCLUSION_SPEC
    assert contract["version"] == 1
    assert contract["key"] == spec.key
    assert contract["base_type"] == spec.base_type == "Event"
    assert contract["name"] == spec.name
    assert contract["description"] == spec.description
    assert contract["fields"] == spec.fields
    assert tuple(contract["required_fields"]) == spec.required_fields
    assert spec.fingerprint == "92f7c8ea19c922fcafb1e9274e90ef9ff5974bc3e8644973fef9bd1bc5029af0"
