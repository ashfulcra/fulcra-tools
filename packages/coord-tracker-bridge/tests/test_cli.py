import json

import pytest

from coord_tracker_bridge.cli import (
    LINEAR_KEY_ENV_VARS,
    _auth_failure_hint,
    _looks_like_auth_failure,
    _resolve_linear_key,
    _service,
    build_parser,
    main,
)
from coord_tracker_bridge.linear import LinearError, MarkerAdoption
from coord_tracker_bridge.model import SourceIdentity


def test_cli_exposes_only_explicit_gated_phases():
    parser = build_parser()

    for phase in ("plan", "adopt-markers", "apply-resources", "sync"):
        assert parser.parse_args([phase, "--linear-team-id", "team"]).phase == phase

    assert parser.parse_args(["plan", "--source", "teams"]).source == "teams"


def test_cli_fails_loud_without_linear_credentials(monkeypatch, capsys):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    assert main(["plan", "--linear-team-id", "team"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err


def test_cli_rejects_dry_run_for_non_adoption_phase(monkeypatch, capsys):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")

    assert main(["plan", "--dry-run", "--linear-team-id", "team"]) == 2
    assert "only valid with adopt-markers" in capsys.readouterr().err


def test_cli_adoption_dry_run_emits_mapping_without_mutating(monkeypatch, capsys):
    source = SourceIdentity("coord-engine", "fulcra/tasks", "task-1")
    adoption = MarkerAdoption("LIN-1", source, "tasks", "Task", "body", {})

    class Service:
        def preview_marker_adoptions(self):
            return (adoption,)

        def adopt_markers(self):
            raise AssertionError("mutating path must not run")

    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    monkeypatch.setattr("coord_tracker_bridge.cli._service", lambda _args: Service())

    assert main(["adopt-markers", "--dry-run", "--linear-team-id", "team"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "adoptions": [{
            "capability": "tasks",
            "provider_id": "LIN-1",
            "source": source.to_dict(),
        }],
        "count": 1,
        "dry_run": True,
    }


def test_source_modes_use_distinct_ledger_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    parser = build_parser()
    common = ["plan", "--linear-team-id", "linear-team", "--state-dir", str(tmp_path)]

    engine = _service(parser.parse_args([*common, "--source", "engine"]))
    teams = _service(parser.parse_args([*common, "--source", "teams"]))

    assert engine.ledger_path != teams.ledger_path


def test_cli_exposes_linear_assignments_and_defaults_to_preview():
    """`--deliver` is both the dispatch flag and the consume flag, so the
    default has to be the one that does neither."""
    parser = build_parser()
    args = parser.parse_args(["linear-assignments", "--linear-team-id", "team"])
    assert args.phase == "linear-assignments"
    assert args.deliver is False and args.seed is False
    assert args.coordinator == "coord-boss"


def test_cli_rejects_deliver_outside_the_assignments_phase(monkeypatch, capsys):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")

    assert main(["sync", "--deliver", "--linear-team-id", "team"]) == 2
    assert "only valid with linear-assignments" in capsys.readouterr().err


def test_cli_assignments_fails_loud_without_linear_credentials(monkeypatch, capsys):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    assert main(["linear-assignments", "--linear-team-id", "team"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err


# --- credential resolution -------------------------------------------------
# The bridge read LINEAR_API_KEY and only LINEAR_API_KEY. That credential
# stopped authenticating and the Linear projection went stale for a month
# while working credentials sat unused in the same environment.

def test_a_working_credential_is_found_even_when_the_documented_one_is_absent(monkeypatch):
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "personal")
    assert _resolve_linear_key() == ("LINEAR_PERSONAL_KEY", "personal")


def test_resolution_order_prefers_a_personal_key_over_LINEAR_API_KEY(monkeypatch):
    """THE REGRESSION: both present, and the bridge must not pick the one
    whose only distinction is that it is the one the docs named."""
    monkeypatch.setenv("LINEAR_API_KEY", "bearer-oauth")
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "personal")
    assert _resolve_linear_key()[0] == "LINEAR_PERSONAL_KEY"


def test_LINEAR_KEY_ENV_names_the_variable_to_use(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "bearer-oauth")
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "personal")
    monkeypatch.setenv("LINEAR_KEY_ENV", "LINEAR_API_KEY")
    assert _resolve_linear_key() == ("LINEAR_API_KEY", "bearer-oauth")


def test_an_empty_credential_is_not_a_credential(monkeypatch):
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "   ")
    monkeypatch.setenv("LINEAR_API_KEY", "real")
    assert _resolve_linear_key() == ("LINEAR_API_KEY", "real")


def test_no_credential_at_all_names_every_variable_it_looked_for(monkeypatch):
    with pytest.raises(LinearError) as excinfo:
        _resolve_linear_key()
    for name in LINEAR_KEY_ENV_VARS:
        assert name in str(excinfo.value)


def test_an_auth_failure_names_the_variable_used_and_the_ones_it_skipped(monkeypatch):
    """A bare 401 is what made this take a month to find. The hint has to say
    which credential was sent, and that others were sitting there."""
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "personal")
    monkeypatch.setenv("LINEAR_API_KEY", "bearer-oauth")
    hint = _auth_failure_hint()
    assert "LINEAR_PERSONAL_KEY" in hint
    assert "LINEAR_API_KEY" in hint
    assert "LINEAR_KEY_ENV" in hint


def test_the_hint_never_carries_a_credential_value(monkeypatch):
    monkeypatch.setenv("LINEAR_PERSONAL_KEY", "lin_api_supersecret")
    monkeypatch.setenv("LINEAR_API_KEY", "Bearer lin_oauth_alsosecret")
    hint = _auth_failure_hint()
    assert "supersecret" not in hint and "alsosecret" not in hint


def test_only_auth_shaped_failures_get_the_credential_hint():
    """An outage must not be reported as a credential problem -- that is the
    exact mis-attribution this whole change exists to stop."""
    assert _looks_like_auth_failure("sync: Linear request failed (http_status=401)")
    assert _looks_like_auth_failure("sync: failed (http_status=403)")
    assert _looks_like_auth_failure("sync: failed, graphql_codes=AUTHENTICATION_ERROR")
    assert not _looks_like_auth_failure("sync: Linear request failed (http_status=500)")
    assert not _looks_like_auth_failure("sync: transport failed (transport=ConnectError)")
