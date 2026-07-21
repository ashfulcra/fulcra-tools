import json

from coord_tracker_bridge.cli import _service, build_parser, main
from coord_tracker_bridge.linear import MarkerAdoption
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
