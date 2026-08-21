"""Unit 6 migration and release gates, written before their implementation."""

from __future__ import annotations

import builtins
import json
from argparse import Namespace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

import pytest

from coord_engine import classifier, migration, pin_currency
from coord_engine import cli


def test_v1_bootstrap_is_input_only_and_never_claims_v2_authority():
    result = migration.read_v1_bootstrap(json.dumps({
        "schema": "coord.teams.summaries.v1",
        "rows": [{"name": "one", "status": "active"}],
        "reviews": {"schema": "coord.reviews.projection.v3", "complete": True,
                    "rows": [], "orphans": [], "orphans_unknown": [],
                    "tombstones": []},
        "forge": {"schema": "coord.forge.projection.v1", "complete": True,
                  "responsible": {}, "feedback": {}},
    }))

    assert result.state == "DATA"
    assert result.authoritative is False
    assert result.sections["tasks"] == {"rows": [{"name": "one", "status": "active"}]}
    assert result.sections["reviews"]["complete"] is True
    assert result.sections["forge"]["complete"] is True


def test_malformed_v1_bootstrap_is_unknown_not_empty():
    result = migration.read_v1_bootstrap('{"schema":"coord.teams.summaries.v1"}')
    assert result.state == "UNKNOWN"
    assert result.sections == {}


def test_unstamped_early_v1_bootstrap_remains_readable_for_downgrade_rebuilds():
    result = migration.read_v1_bootstrap(json.dumps({
        "rows": [{"name": "one", "status": "active"}],
    }))

    assert result.state == "DATA"
    assert result.authoritative is False
    assert result.sections["tasks"]["rows"][0]["name"] == "one"


def test_explicit_non_v1_bootstrap_schema_fails_closed():
    result = migration.read_v1_bootstrap(json.dumps({
        "schema": "coord.teams.summaries.v2",
        "rows": [],
    }))

    assert result.state == "UNKNOWN"


EVIDENCE_AT = "2026-08-21T00:00:00Z"
AUTHORITY = {
    "data_type": "MomentAnnotation/d04f357e-b556-4298-ad1e-4ce307d54041",
    "api_version": "v1alpha1",
}
BUILD_SHA = "c3f4680a93a135520b6ffaf767ef46e1fe97a798"
LIVE_ENVELOPE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "live_data_updates_2026-08-20T2013Z.min.json"
)


@pytest.fixture(autouse=True)
def _stable_measurement_build(monkeypatch):
    monkeypatch.setattr(pin_currency, "build_sha", lambda: BUILD_SHA)


def _binding(row):
    payload = {key: value for key, value in row.items()
               if key != "credential_provenance"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "evidence-sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _host(
    name: str, *, credentialed: bool = True, label: str | None = None,
    principal_source: str = "env",
):
    probe_id = hashlib.md5(name.encode()).hexdigest()
    row = {
        "schema": "coord.feed-visibility-lag.v1",
        "state": "DATA",
        "team": "fulcra",
        "host_identity": f"coord-reconcile:{name}",
        "display_label": label or name,
        "principal_identity": f"agent-{name}",
        "principal_source": principal_source,
        "transport_authority": dict(AUTHORITY),
        "probe_schema": "coord.feed-visibility-lag-probe.v1",
        "producer_build": BUILD_SHA,
        "credential_provenance": None,
        "credentialed": credentialed,
        "observed_seconds": 2.5,
        "measured_at": EVIDENCE_AT,
        "probe_id": probe_id,
        "probe_path": f"team/fulcra/_coord/projections/lag-probes/{probe_id}.json",
        "event_at": "2026-08-21T00:00:01Z",
        "observed_at": "2026-08-21T00:00:03.500000Z",
        "update_id": "3185bd09-9500-4407-bd87-013832fe55f3",
        "reason": None,
    }
    row["credential_provenance"] = _binding(row)
    return row


def _cohort_row(name, *, team="fulcra", authority=None):
    row = _host(name)
    row["team"] = team
    row["transport_authority"] = dict(authority or AUTHORITY)
    row["probe_path"] = (
        f"team/{team}/_coord/projections/lag-probes/{row['probe_id']}.json"
    )
    row["credential_provenance"] = _binding(row)
    return row


def _exclusions():
    return {
        name: ({"reason": reason,
                "provenance": "Ash operator ruling",
                "host_identity": name}
               if name == "MacBookPro.localdomain"
               else {"reason": reason,
                     "provenance": "measured fleet census",
                     "host_identity": name,
                     "last_reconciled_at": "2026-08-20T00:00:00Z",
                     "age_seconds": 86400})
        for name, reason in migration.REQUIRED_EXCLUSIONS.items()
    }


def _fleet(*, host_b_version="2.0.0"):
    return {
        "host-a": {"host_identity": "host-a", "engine_version": "2.0.0",
                   "age_seconds": 30,
                   "last_reconciled_at": "2026-08-20T23:59:30Z"},
        "host-b": {"host_identity": "host-b", "engine_version": host_b_version,
                   "age_seconds": 30,
                   "last_reconciled_at": "2026-08-20T23:59:30Z"},
        "MacBookPro.localdomain": {
            "host_identity": "MacBookPro.localdomain",
            "engine_version": "1.6.9", "age_seconds": 30,
            "last_reconciled_at": "2026-08-20T23:59:30Z",
        },
    }


def test_unmeasured_or_one_host_epsilon_fails_closed():
    for hosts in ([], [_host("host-a")]):
        decision = migration.evaluate_activation(
            hosts=hosts, configured_epsilon_seconds=3.0,
            fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
            exclusions=_exclusions(),
            cas_supported=True,
        )
        assert decision.state == "UNKNOWN"
        assert decision.ready is False


def test_mixed_fleet_and_schema2_without_cas_are_refused():
    measured = [_host("host-a"), _host("host-b")]
    mixed = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(host_b_version="1.11.0"), fleet_sla_seconds=300,
        evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(),
        cas_supported=True,
    )
    no_cas = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(),
        cas_supported=False,
    )
    assert (mixed.state, mixed.ready) == ("REFUSED", False)
    assert (no_cas.state, no_cas.ready) == ("REFUSED", False)


def test_activation_requires_named_exclusions_and_epsilon_above_measurement():
    measured = [_host("host-a"), _host("host-b")]
    missing_exclusions = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions={}, cas_supported=True,
    )
    too_small = migration.evaluate_activation(
        hosts=measured, configured_epsilon_seconds=2.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert missing_exclusions.state == "UNKNOWN"
    assert too_small.state == "REFUSED"


def test_two_credentialed_hosts_can_prove_the_phase1_gate_without_claiming_release():
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert decision.state == "READY"
    assert decision.ready is True
    assert decision.release_complete is False


def test_display_labels_cannot_turn_one_attested_machine_into_two_hosts():
    first = _host("same-machine", label="host-a")
    second = _host("same-machine", label="host-b")
    decision = migration.evaluate_activation(
        hosts=[first, second], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "two distinct credentialed host measurements required" in decision.reasons


def test_activation_rejects_mixed_team_and_transport_authority_cohort():
    other_authority = {
        "data_type": "MomentAnnotation/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "api_version": "v1alpha1",
    }
    decision = migration.evaluate_activation(
        hosts=[_cohort_row("host-a"), _cohort_row(
            "host-b", team="other-team", authority=other_authority,
        )],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "measurement cohort mismatch: canonical team" in decision.reasons
    assert "measurement cohort mismatch: transport authority" in decision.reasons


@pytest.mark.parametrize("authority", [
    {"data_type": "MomentAnnotation/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
     "api_version": "v1alpha1"},
    {"data_type": AUTHORITY["data_type"], "api_version": "V1ALPHA1"},
    {"data_type": AUTHORITY["data_type"] + " ", "api_version": "v1alpha1"},
])
def test_activation_rejects_different_or_caller_normalized_authority(authority):
    decision = migration.evaluate_activation(
        hosts=[_cohort_row("host-a"), _cohort_row("host-b", authority=authority)],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert any("transport authority" in reason for reason in decision.reasons)


def test_activation_rejects_noncanonical_team_case_without_aliasing():
    decision = migration.evaluate_activation(
        hosts=[_cohort_row("host-a"), _cohort_row("host-b", team="Fulcra")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert any("canonical team" in reason for reason in decision.reasons)


def test_activation_rejects_different_producer_builds_in_one_cohort():
    second = _cohort_row("host-b")
    second["producer_build"] = "d" * 40
    second["credential_provenance"] = _binding(second)
    decision = migration.evaluate_activation(
        hosts=[_cohort_row("host-a"), second], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "measurement cohort mismatch: producer build" in decision.reasons


def test_activation_compares_every_versioned_authority_field():
    first_authority = {
        **AUTHORITY,
        "protocol_version": 1,
        "cursor_schema_version": 1,
        "minimum_reader_version": "1.8.0",
        "minimum_writer_version": "1.8.0",
        "cursor_generation": 0,
        "cursor_activated_at": None,
    }
    second_authority = {**first_authority, "minimum_writer_version": "1.9.0"}
    decision = migration.evaluate_activation(
        hosts=[
            _cohort_row("host-a", authority=first_authority),
            _cohort_row("host-b", authority=second_authority),
        ],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "measurement cohort mismatch: transport authority" in decision.reasons


def test_measurement_host_identity_must_have_canonical_machine_shape():
    forged = _host("host-a")
    forged["host_identity"] = "caller-controlled-label"
    decision = migration.evaluate_activation(
        hosts=[forged, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_activation_consumes_exact_lag_measurement_schema_not_synthetic_maximum():
    synthetic = _host("host-a")
    synthetic["observed_max_seconds"] = synthetic.pop("observed_seconds")
    synthetic["credential_provenance"] = _binding(synthetic)
    decision = migration.evaluate_activation(
        hosts=[synthetic, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


@pytest.mark.parametrize("missing", [
    "event_at", "observed_at", "update_id", "probe_path", "transport_authority",
])
def test_handcrafted_measurement_missing_lifecycle_or_probe_evidence_is_unknown(missing):
    row = _host("host-a")
    row.pop(missing)
    row["credential_provenance"] = _binding(row)
    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_arbitrary_sha256_provenance_cannot_pass_measurement_gate():
    row = _host("host-a")
    row["credential_provenance"] = "evidence-sha256:" + "a" * 64
    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_non_json_measurement_field_is_unknown_not_an_exception():
    row = _host("host-a")
    row["display_label"] = object()
    row["credential_provenance"] = "evidence-sha256:" + "a" * 64

    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


@pytest.mark.parametrize(("field", "value"), [
    ("last_reconciled_at", "not-a-time"),
    ("last_reconciled_at", "2026-08-20T23:59:30"),
    ("age_seconds", float("nan")),
    ("age_seconds", float("inf")),
    ("age_seconds", -1),
    ("age_seconds", 10),
])
def test_fleet_evidence_rejects_malformed_nonfinite_negative_or_inconsistent_age(
        field, value):
    fleet = _fleet()
    fleet["host-a"][field] = value
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=fleet, fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_exclusion_requires_ruling_provenance_and_consistent_age():
    exclusions = _exclusions()
    exclusions["MacBookPro.localdomain"]["provenance"] = ""
    exclusions["coord-boss"]["age_seconds"] = float("nan")
    decision = migration.evaluate_activation(
        hosts=[_host("host-a"), _host("host-b")],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=exclusions, cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_identity_uncertainty_is_typed_instead_of_raising():
    outcome = classifier.resolve_identity_outcome(
        environ={},
        persisted=classifier.PersistedIdentity(
            classifier.PersistedIdentityState.UNKNOWN),
        hostname=lambda: "host-a",
    )
    assert outcome.state == "UNKNOWN"
    assert outcome.identity is None


def test_cli_identity_uncertainty_is_unknown_rc3_not_runtime_error(
        tmp_path, monkeypatch, capsys):
    path = tmp_path / "state"
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(path))
    monkeypatch.chdir(tmp_path)
    # Use the real persisted-identity path and make it unreadable-as-a-file.
    from coord_engine import config
    config.identity_path().mkdir(parents=True)

    class T:
        def write(self, *_args):
            raise AssertionError("identity doubt must stop before a store write")

    rc = cli.main(["presence", "beat", "fulcra", "-s", "working"], T())
    captured = capsys.readouterr()
    assert rc == 3
    assert "identity UNKNOWN" in captured.err
    assert "RuntimeError" not in captured.err


class _Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class _LagTransport:
    def __init__(self, visible_after: float, clock: _Clock):
        self.visible_after = visible_after
        self.clock = clock
        self.writes = []

    def read_classified(self, _path, *, deadline=None):
        return json.dumps(AUTHORITY), "ok"

    def write(self, path, content, *, deadline=None):
        self.writes.append((path, content))
        return True

    def data_updates(self, since, *, deadline=None):
        if self.clock.value < self.visible_after:
            return {"after": since, "through": since, "file_changes": []}
        path = self.writes[-1][0]
        return {"after": since, "through": "2026-08-21T00:00:10Z",
                "file_changes": [{"id": "3185bd09-9500-4407-bd87-013832fe55f3",
                                  "path": path,
                                  "state": "uploaded",
                                  "uploaded_at": "2026-08-21T00:00:01Z"}]}


class _Now:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        second = 0 if self.calls == 1 else 2
        return datetime(2026, 8, 21, 0, 0, second, tzinfo=timezone.utc)


def _data_measurement():
    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, _Clock()), "fulcra", "display",
        environ={"FULCRA_COORD_AGENT": "agent-a"},
        persisted=lambda: pytest.fail("env identity must not consult persisted state"),
        hostname=lambda: "same-machine", timeout_seconds=1.0, poll_seconds=0.001,
        now=_Now(),
    )
    assert result.state == "DATA"
    return result


def test_feed_visibility_measurement_is_bounded_and_reports_observed_lag():
    clock = _Clock()
    transport = _LagTransport(0.3, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        environ={"FULCRA_COORD_AGENT": "agent-a"},
        persisted=lambda: pytest.fail("env identity must not consult persisted state"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=_Now(),
    )
    assert result.state == "DATA"
    assert result.observed_seconds == 1.0
    assert result.credentialed is True
    assert result.host_identity == "coord-reconcile:same-machine"
    assert result.principal_identity == "agent-a"
    assert result.principal_source == "env"
    assert result.event_at == "2026-08-21T00:00:01Z"
    assert result.as_dict().get("probe_schema") == "coord.feed-visibility-lag-probe.v1"
    assert result.as_dict().get("producer_build") == BUILD_SHA
    assert "observed_max_seconds" not in result.as_dict()
    assert result.as_dict()["credential_provenance"] == _binding(result.as_dict())
    probe = json.loads(transport.writes[0][1])
    assert probe["principal_identity"] == "agent-a"
    assert probe["principal_source"] == "env"

    decision = migration.evaluate_activation(
        hosts=[result.as_dict(), _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )
    assert decision.state == "READY"


def test_lag_measurement_uses_positive_probe_row_from_exact_live_envelope():
    """Removing synthetic envelope boundaries must not break point observation."""
    fixture = json.loads(LIVE_ENVELOPE_FIXTURE.read_text())
    event_at = "2026-08-20T20:04:27.095791Z"

    class LiveEnvelopeTransport(_LagTransport):
        def __init__(self):
            super().__init__(0, _Clock())
            self.feed_requests = []

        def data_updates(self, since, *, deadline=None):
            self.feed_requests.append(since)
            envelope = json.loads(json.dumps(fixture))
            envelope["file_changes"][0]["full_name"] = "/" + self.writes[-1][0]
            return envelope

    class LiveNow:
        def __init__(self):
            self.values = iter((
                datetime(2026, 8, 20, 20, 4, 26, tzinfo=timezone.utc),
                datetime(2026, 8, 20, 20, 4, 29, 95791, tzinfo=timezone.utc),
            ))

        def __call__(self):
            return next(self.values)

    transport = LiveEnvelopeTransport()
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        environ={"FULCRA_COORD_AGENT": "agent-a"},
        persisted=lambda: pytest.fail("env identity must not consult persisted state"),
        hostname=lambda: "same-machine", timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=transport.clock.monotonic, sleep=transport.clock.sleep,
        now=LiveNow(),
    )

    assert result.state == "DATA"
    assert result.update_id == fixture["file_changes"][0]["id"]
    assert result.event_at == event_at
    assert transport.feed_requests == ["6 seconds"]


@pytest.mark.parametrize("mutation", ["missing", "invalid", "unbound"])
def test_activation_rejects_missing_invalid_or_unbound_principal_source(mutation):
    row = _host("host-a")
    if mutation == "missing":
        del row["principal_source"]
        row["credential_provenance"] = _binding(row)
    elif mutation == "invalid":
        row["principal_source"] = "hostname"
        row["credential_provenance"] = _binding(row)
    else:
        row["principal_source"] = "persisted"

    decision = migration.evaluate_activation(
        hosts=[row, _host("host-b")], configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert decision.reasons


def test_activation_rejects_mixed_principal_source_cohort():
    persisted = _host("host-b", principal_source="persisted")
    decision = migration.evaluate_activation(
        hosts=[_host("host-a", principal_source="env"), persisted],
        configured_epsilon_seconds=3.0,
        fleet=_fleet(), fleet_sla_seconds=300, evidence_measured_at=EVIDENCE_AT,
        exclusions=_exclusions(), cas_supported=True,
    )

    assert decision.state == "UNKNOWN"
    assert decision.ready is False
    assert "measurement cohort mismatch: principal source" in decision.reasons


@pytest.mark.parametrize("persisted_state", [
    classifier.PersistedIdentityState.ABSENT,
    classifier.PersistedIdentityState.UNKNOWN,
    classifier.PersistedIdentityState.UNSUPPORTED,
])
def test_measurement_env_principal_wins_lazily_over_every_persisted_state(
        persisted_state):
    transport = _LagTransport(0, _Clock())

    def forbidden_persisted():
        raise AssertionError(f"must not consult {persisted_state.value} persisted state")

    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display",
        environ={"FULCRA_COORD_AGENT": "codex-coder"},
        persisted=forbidden_persisted, hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=0.001, now=_Now(),
    )

    assert result.state == "DATA"
    assert result.rc == 0
    assert result.principal_identity == "codex-coder"
    assert result.principal_source == "env"
    assert json.loads(transport.writes[0][1])["principal_source"] == "env"


def test_measurement_persisted_principal_is_used_when_environment_is_absent():
    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, _Clock()), "fulcra", "display", environ={},
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "persisted-agent"),
        hostname=lambda: "same-machine", timeout_seconds=1.0,
        poll_seconds=0.001, now=_Now(),
    )

    assert result.state == "DATA"
    assert result.rc == 0
    assert result.principal_identity == "persisted-agent"
    assert result.principal_source == "persisted"


@pytest.mark.parametrize("state", [
    classifier.PersistedIdentityState.ABSENT,
    classifier.PersistedIdentityState.UNKNOWN,
    classifier.PersistedIdentityState.UNSUPPORTED,
])
def test_measurement_missing_principal_never_falls_back_to_hostname(state):
    transport = _LagTransport(0, _Clock())
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display", environ={},
        persisted=lambda: classifier.PersistedIdentity(state),
        hostname=lambda: "tempting-host-fallback", timeout_seconds=1.0,
        poll_seconds=0.001, now=_Now(),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3
    assert result.principal_identity is None
    assert result.principal_source is None
    assert transport.writes == []


def test_measurement_explicit_principal_precedes_env_without_cli_flag():
    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, _Clock()), "fulcra", "display",
        explicit_identity="explicit-agent",
        environ={"FULCRA_COORD_AGENT": "env-agent"},
        persisted=lambda: pytest.fail("explicit identity must stay lazy"),
        hostname=lambda: "same-machine", timeout_seconds=1.0,
        poll_seconds=0.001, now=_Now(),
    )

    assert result.state == "DATA"
    assert result.principal_identity == "explicit-agent"
    assert result.principal_source == "explicit"


def test_feed_visibility_measurement_refuses_non_finite_bounds_before_write():
    clock = _Clock()
    transport = _LagTransport(0.3, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "host-a",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=float("inf"),
        monotonic=clock.monotonic, sleep=clock.sleep,
        now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3
    assert transport.writes == []


@pytest.mark.parametrize("envelope", [
    {"files": [{"path": "PROBE"}]},
    {"through": "2026-08-21T00:00:10Z", "file_changes": []},
    {"after": "not-a-time", "through": "2026-08-21T00:00:10Z",
     "file_changes": []},
    {"after": "2026-08-21T00:00:00Z", "through": "2016-08-21T00:00:10Z",
     "file_changes": []},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"id": "u", "path": "PROBE",
                       "uploaded_at": "2026-08-21T00:00:01Z"}]},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"path": "PROBE", "state": "uploaded",
                       "uploaded_at": "2026-08-21T00:00:01Z"}]},
    {"after": "2026-08-21T00:00:00Z", "through": "2026-08-21T00:00:10Z",
     "file_changes": [{"id": "u", "path": "PROBE", "state": "uploaded"}]},
])
def test_lag_harness_rejects_unattested_or_malformed_feed_body_and_rc(envelope):
    clock = _Clock()

    class Malformed(_LagTransport):
        def data_updates(self, since, *, deadline=None):
            path = self.writes[-1][0]
            return json.loads(json.dumps(envelope).replace("PROBE", path))

    transport = Malformed(0, clock)
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display-only",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine",
        timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep, now=_Now(),
    )

    body = result.as_dict()
    assert body["state"] == "UNKNOWN"
    assert body["reason"]
    assert result.rc == 3


def test_cli_host_id_is_display_only_and_cannot_mint_attested_identity(
        monkeypatch, capsys):
    captured = []

    def capture_measure(_transport, _team, display_label, *, persisted, hostname,
                        **_kwargs):
        saved = persisted()
        captured.append((display_label, hostname(), saved.identity))
        return migration.LagMeasurement.unknown(
            display_label=display_label, reason="test stop",
            measured_at=EVIDENCE_AT, probe_id="probe",
        )

    transport = _LagTransport(0, _Clock())
    monkeypatch.setattr(cli.config, "persisted_identity", lambda: classifier.PersistedIdentity(
        classifier.PersistedIdentityState.PRESENT, "agent-a"))
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "same-machine")
    monkeypatch.setattr(migration, "measure_feed_visibility_lag", capture_measure)
    for label in ("host-a", "host-b"):
        rc = cli.cmd_measure_feed_lag(
            Namespace(team="fulcra", host_id=label, timeout=1.0, poll=0.1),
            transport,
        )
        assert rc == 3
        capsys.readouterr()

    assert captured == [
        ("host-a", "same-machine", "agent-a"),
        ("host-b", "same-machine", "agent-a"),
    ]


def test_cli_env_principal_succeeds_with_absent_persisted_identity(
        monkeypatch, capsys):
    class CurrentTransport(_LagTransport):
        def data_updates(self, since, *, deadline=None):
            event_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return {
                "file_changes": [{
                    "id": "3185bd09-9500-4407-bd87-013832fe55f3",
                    "path": self.writes[-1][0], "state": "uploaded",
                    "uploaded_at": event_at,
                }],
            }

    monkeypatch.setenv("FULCRA_COORD_AGENT", "codex-coder")
    monkeypatch.setattr(
        cli.config, "persisted_identity",
        lambda: pytest.fail("env authority must not consult absent persisted state"),
    )
    rc = cli.cmd_measure_feed_lag(
        Namespace(team="fulcra", host_id="display", timeout=1.0, poll=0.001),
        CurrentTransport(0, _Clock()),
    )
    body = json.loads(capsys.readouterr().out)

    assert rc == 0, body["reason"]
    assert body["state"] == "DATA"
    assert body["principal_identity"] == "codex-coder"
    assert body["principal_source"] == "env"


@pytest.mark.parametrize("auth_mode", ["cli", "http"])
def test_token_refresh_or_auth_mode_is_not_credential_provenance(auth_mode):
    clock = _Clock()

    class RotatingTokenTransport(_LagTransport):
        mode = auth_mode

        def _access_token(self):
            raise AssertionError("bearer token must not be read or hashed")

    result = migration.measure_feed_visibility_lag(
        RotatingTokenTransport(0.3, clock), "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=1.0, poll_seconds=0.1,
        monotonic=clock.monotonic, sleep=clock.sleep, now=_Now(),
    )
    assert result.state == "DATA"
    assert result.rc == 0


def test_missing_exact_producer_build_is_unknown_before_write(monkeypatch):
    transport = _LagTransport(0, _Clock())
    monkeypatch.setattr(pin_currency, "build_sha", lambda: None)

    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=1.0, poll_seconds=0.001,
        now=_Now(),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3
    assert transport.writes == []


@pytest.mark.parametrize("slow_phase", ["auth", "write"])
def test_entire_harness_timeout_covers_slow_preflight_and_upload(slow_phase):
    class Slow(_LagTransport):
        def read_classified(self, path, *, deadline=None):
            if slow_phase == "auth":
                time.sleep(0.08)
            return super().read_classified(path, deadline=deadline)

        def write(self, path, content, *, deadline=None):
            if slow_phase == "write":
                time.sleep(0.08)
            return super().write(path, content, deadline=deadline)

    transport = Slow(0, _Clock())
    started = time.monotonic()
    result = migration.measure_feed_visibility_lag(
        transport, "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=0.01, poll_seconds=0.001,
    )
    elapsed = time.monotonic() - started

    assert result.as_dict()["state"] == "UNKNOWN"
    assert result.rc == 3
    assert elapsed < 0.20
    if slow_phase == "auth":
        assert transport.writes == []


def test_final_observation_overrun_is_unknown_not_data():
    class SlowFinalNow:
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if self.calls == 2:
                time.sleep(0.08)
            second = 0 if self.calls == 1 else 2
            return datetime(2026, 8, 21, 0, 0, second, tzinfo=timezone.utc)

    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, _Clock()), "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=0.02, poll_seconds=0.001,
        now=SlowFinalNow(),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3


def test_final_measurement_serialization_overrun_is_unknown(monkeypatch):
    real_dumps = migration.json.dumps

    def slow_measurement_dumps(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("schema") == "coord.feed-visibility-lag.v1":
            time.sleep(0.08)
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(migration.json, "dumps", slow_measurement_dumps)
    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, _Clock()), "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=0.02, poll_seconds=0.001,
        now=_Now(),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3


def test_exact_final_deadline_boundary_is_expired():
    clock = _Clock()

    class BoundaryNow(_Now):
        def __call__(self):
            value = super().__call__()
            if self.calls == 2:
                clock.value = 0.02
            return value

    result = migration.measure_feed_visibility_lag(
        _LagTransport(0, clock), "fulcra", "display",
        persisted=lambda: classifier.PersistedIdentity(
            classifier.PersistedIdentityState.PRESENT, "agent-a"),
        hostname=lambda: "same-machine", timeout_seconds=0.02, poll_seconds=0.001,
        monotonic=clock.monotonic, sleep=clock.sleep, now=BoundaryNow(),
    )

    assert result.state == "UNKNOWN"
    assert result.rc == 3


def test_cli_renderer_overrun_replaces_data_with_unknown(monkeypatch, capsys):
    measurement = _data_measurement()
    real_dumps = cli.jsonutil.dumps

    def slow_dumps(value):
        if isinstance(value, dict) and value.get("state") == "DATA":
            time.sleep(0.08)
        return real_dumps(value)

    monkeypatch.setattr(
        migration, "measure_feed_visibility_lag",
        lambda *_args, **_kwargs: measurement,
    )
    monkeypatch.setattr(cli.jsonutil, "dumps", slow_dumps)
    rc = cli.cmd_measure_feed_lag(
        Namespace(team="fulcra", host_id="display", timeout=0.02, poll=0.001),
        _LagTransport(0, _Clock()),
    )
    body = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert body["state"] == "UNKNOWN"
    assert body["reason"]


def test_cli_commits_body_and_rc_before_stdout_without_post_print_downgrade(
        monkeypatch, capsys):
    measurement = _data_measurement()

    class FalseThenTrue:
        def __init__(self):
            self.calls = 0

        def expired(self):
            self.calls += 1
            return self.calls > 1

    deadline = FalseThenTrue()
    monkeypatch.setattr(
        cli.budget_mod.Deadline, "open", classmethod(lambda _cls, _seconds: deadline),
    )
    monkeypatch.setattr(
        migration, "measure_feed_visibility_lag",
        lambda *_args, **_kwargs: measurement,
    )

    rc = cli.cmd_measure_feed_lag(
        Namespace(team="fulcra", host_id="display", timeout=0.02, poll=0.001),
        _LagTransport(0, _Clock()),
    )
    output = capsys.readouterr().out
    body = json.loads(output)

    assert body["state"] == "DATA"
    assert rc == 0
    assert deadline.calls == 1
    assert output.count("\n") == 1


def test_cli_serialization_boundary_expiry_commits_unknown_once(monkeypatch, capsys):
    measurement = _data_measurement()

    class ExpiredAtDecision:
        def __init__(self):
            self.calls = 0

        def expired(self):
            self.calls += 1
            return True

    deadline = ExpiredAtDecision()
    monkeypatch.setattr(
        cli.budget_mod.Deadline, "open", classmethod(lambda _cls, _seconds: deadline),
    )
    monkeypatch.setattr(
        migration, "measure_feed_visibility_lag",
        lambda *_args, **_kwargs: measurement,
    )

    rc = cli.cmd_measure_feed_lag(
        Namespace(team="fulcra", host_id="display", timeout=0.02, poll=0.001),
        _LagTransport(0, _Clock()),
    )
    output = capsys.readouterr().out
    body = json.loads(output)

    assert body["state"] == "UNKNOWN"
    assert rc == 3
    assert deadline.calls == 1
    assert output.count("\n") == 1


def test_cli_slow_stdout_does_not_retroactively_change_committed_data(
        monkeypatch, capsys):
    measurement = _data_measurement()

    class FalseThenTrue:
        def __init__(self):
            self.calls = 0

        def expired(self):
            self.calls += 1
            return self.calls > 1

    deadline = FalseThenTrue()
    real_print = builtins.print

    def slow_print(*args, **kwargs):
        time.sleep(0.03)
        return real_print(*args, **kwargs)

    monkeypatch.setattr(
        cli.budget_mod.Deadline, "open", classmethod(lambda _cls, _seconds: deadline),
    )
    monkeypatch.setattr(
        migration, "measure_feed_visibility_lag",
        lambda *_args, **_kwargs: measurement,
    )
    monkeypatch.setattr(builtins, "print", slow_print)

    rc = cli.cmd_measure_feed_lag(
        Namespace(team="fulcra", host_id="display", timeout=0.02, poll=0.001),
        _LagTransport(0, _Clock()),
    )
    output = capsys.readouterr().out
    body = json.loads(output)

    assert body["state"] == "DATA"
    assert rc == 0
    assert deadline.calls == 1
    assert output.count("\n") == 1
