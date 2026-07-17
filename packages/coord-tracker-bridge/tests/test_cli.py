from coord_tracker_bridge.cli import _service, build_parser, main


def test_cli_exposes_only_explicit_gated_phases():
    parser = build_parser()

    for phase in ("plan", "adopt-markers", "apply-resources", "sync"):
        assert parser.parse_args([phase, "--linear-team-id", "team"]).phase == phase

    assert parser.parse_args(["plan", "--source", "teams"]).source == "teams"


def test_cli_fails_loud_without_linear_credentials(monkeypatch, capsys):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    assert main(["plan", "--linear-team-id", "team"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err


def test_source_modes_use_distinct_ledger_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    parser = build_parser()
    common = ["plan", "--linear-team-id", "linear-team", "--state-dir", str(tmp_path)]

    engine = _service(parser.parse_args([*common, "--source", "engine"]))
    teams = _service(parser.parse_args([*common, "--source", "teams"]))

    assert engine.ledger_path != teams.ledger_path
