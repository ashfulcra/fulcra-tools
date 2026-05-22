"""The plugin API types."""
from __future__ import annotations

from datetime import timedelta

import pytest

from fulcra_collect.plugin import Credential, Permission, Plugin


def _noop(ctx) -> None:
    pass


def test_scheduled_plugin_requires_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="scheduled", run=_noop)


def test_non_scheduled_plugin_rejects_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="manual", run=_noop,
               default_interval=timedelta(hours=1))


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        Plugin(id="x", name="X", kind="weekly", run=_noop)


def test_valid_plugins_of_each_kind():
    svc = Plugin(id="relay", name="Relay", kind="service", run=_noop)
    sch = Plugin(id="lastfm", name="Last.fm", kind="scheduled", run=_noop,
                 default_interval=timedelta(hours=1))
    man = Plugin(id="dayone", name="Day One", kind="manual", run=_noop)
    assert svc.kind == "service"
    assert sch.default_interval == timedelta(hours=1)
    assert man.kind == "manual"


def test_permission_and_credential_are_simple_records():
    p = Permission(id="full-disk-access", explanation="needed to read the DB")
    c = Credential(key="lastfm-api-key", label="Last.fm API key",
                   help="https://www.last.fm/api/account/create")
    assert p.id == "full-disk-access"
    assert c.key == "lastfm-api-key"


def test_requires_network_defaults_true_and_is_overridable():
    online = Plugin(id="x", name="X", kind="manual", run=_noop)
    offline_ok = Plugin(id="y", name="Y", kind="manual", run=_noop,
                        requires_network=False)
    assert online.requires_network is True
    assert offline_ok.requires_network is False
