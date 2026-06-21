from fulcra_okf.ext import NAMESPACE, namespaced, is_namespaced, REGISTRY, ExtField


def test_namespaced_prefixes_and_is_idempotent():
    assert namespaced("weight") == "x_fulcra_weight"
    assert namespaced("x_fulcra_weight") == "x_fulcra_weight"
    assert NAMESPACE == "x_fulcra_"


def test_is_namespaced():
    assert is_namespaced("x_fulcra_weight")
    assert not is_namespaced("title")


def test_registry_entries_well_formed():
    assert "x_fulcra_consent_audience" in REGISTRY
    for key, entry in REGISTRY.items():
        assert key.startswith(NAMESPACE)
        assert isinstance(entry, ExtField)
        assert entry.status in ("proposed", "accepted", "standard")
