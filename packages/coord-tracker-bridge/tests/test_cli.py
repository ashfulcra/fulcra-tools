from coord_tracker_bridge.cli import build_parser, main


def test_cli_exposes_only_three_gated_phases():
    parser = build_parser()

    for phase in ("plan", "apply-resources", "sync"):
        assert parser.parse_args([phase, "--linear-team-id", "team"]).phase == phase


def test_cli_fails_loud_without_linear_credentials(monkeypatch, capsys):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    assert main(["plan", "--linear-team-id", "team"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err
