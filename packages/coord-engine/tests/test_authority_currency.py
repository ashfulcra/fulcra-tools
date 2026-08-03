"""A machine-replacement snapshot restores an old engine; that engine's first
bus read must say so. Zero transport cost: the check rides the config the
queue already loads (RCA 2026-08-02, links 1-2).

BOOTSTRAP LIMITATION, stated for the record: an engine old enough to predate
this check cannot warn about itself — it does not contain the code. Snapshot
repair is the prerequisite that makes currency checking effective; what this
buys is every FUTURE skew, not the engine that is stale today.

``doctor --self`` is TRI-STATE on purpose. rc 0 `current` is claimed ONLY when
the authority declares a pin, the pin parses, and this engine is at or above
it. An absent or malformed pin, or an unreadable config, is rc 2 `unknown`:
"comparison impossible" is not "current", and a health check that reports
green because it could not look is worse than no check.
"""
import json

import pytest

from coord_engine import __version__, cli, records
from coord_engine_test_helpers import FakeTransport

TEAM = "r"


def test_stale_engine_gets_one_actionable_warning():
    cfg = {"current_engine_version": "1.10.0"}
    w = records.authority_currency(cfg, engine_version="1.9.0")
    assert w is not None and "1.9.0" in w and "1.10.0" in w
    assert "adopt-latest" in w


def test_current_engine_is_silent():
    cfg = {"current_engine_version": "1.10.0"}
    assert records.authority_currency(cfg, engine_version="1.10.0") is None


def test_absent_field_or_config_is_silent_not_guessed():
    # Legacy authority without the field: no warning — absence of the pin is
    # not evidence of staleness (fail-closed applies to work, not nagging).
    assert records.authority_currency({}, engine_version="1.9.0") is None
    assert records.authority_currency(None, engine_version="1.9.0") is None


def test_newer_engine_than_authority_is_silent():
    # A dev engine ahead of the pin must not nag every read.
    cfg = {"current_engine_version": "1.9.0"}
    assert records.authority_currency(cfg, engine_version="1.10.0") is None


# --- doctor --self: the tri-state health check -------------------------------

def _doctor(capsys, transport, argv=("--self",)):
    rc = cli.main(["doctor", TEAM, *argv], transport=transport)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def _transport(pin=None, *, config=True):
    t = FakeTransport()
    if config:
        doc = {"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}
        if pin is not None:
            doc["current_engine_version"] = pin
        t.put(records.config_path(TEAM), json.dumps(doc))
    return t


def test_doctor_self_reports_current_when_the_pin_is_met(capsys):
    rc, text = _doctor(capsys, _transport(__version__))
    assert rc == 0
    assert "current" in text.lower()


def test_doctor_self_reports_stale_with_the_adopt_line(capsys):
    rc, text = _doctor(capsys, _transport("99.0.0"))
    assert rc == 3
    assert "stale" in text.lower()
    assert "adopt-latest" in text


def test_doctor_self_absent_field_is_unknown_not_current(capsys):
    """The round-2 rejection, pinned: no pin means we could not compare."""
    rc, text = _doctor(capsys, _transport(None))
    assert rc == 2
    assert "unknown" in text.lower()
    assert "current_engine_version" in text
    assert "current" != text.strip().lower()


def test_doctor_self_malformed_version_is_unknown(capsys):
    rc, text = _doctor(capsys, _transport("not-a-version"))
    assert rc == 2
    assert "unknown" in text.lower()
    assert "not-a-version" in text


def test_doctor_self_unreadable_config_is_unknown(capsys):
    rc, text = _doctor(capsys, _transport(config=False))
    assert rc == 2
    assert "unknown" in text.lower()
    assert "config" in text.lower()


@pytest.mark.parametrize("pin,expected_rc", [
    (__version__, 0), ("99.0.0", 3), (None, 2), ("1.x", 2),
])
def test_doctor_self_never_returns_a_fourth_code(capsys, pin, expected_rc):
    assert _doctor(capsys, _transport(pin))[0] == expected_rc


# --- the queue read carries the same warning, for free -----------------------

def test_stale_engine_shouts_on_the_first_queue_read(capsys, monkeypatch):
    """The warning rides the config every queue read already loads."""
    from test_records_write import QueueTransport

    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = QueueTransport(window=[])
    t.put(records.config_path(TEAM), json.dumps({
        "data_type": "MomentAnnotation/x", "api_version": "v1alpha1",
        "current_engine_version": "99.0.0",
    }))
    rc = cli.main(["queue", TEAM, "--agent", "amy"], transport=t)
    err = capsys.readouterr().err
    assert rc == 0, "a stale engine still reads its queue — this is a warning"
    assert "ENGINE STALE" in err
    assert "adopt-latest" in err


def test_current_engine_adds_no_line_to_a_queue_read(capsys, monkeypatch):
    from test_records_write import QueueTransport

    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = QueueTransport(window=[])
    t.put(records.config_path(TEAM), json.dumps({
        "data_type": "MomentAnnotation/x", "api_version": "v1alpha1",
        "current_engine_version": __version__,
    }))
    cli.main(["queue", TEAM, "--agent", "amy"], transport=t)
    assert "ENGINE STALE" not in capsys.readouterr().err
