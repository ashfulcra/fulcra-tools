"""adopt-latest.sh: when FULCRA_COORD_TEAM / FULCRA_COORD_COORDINATOR are unset the
identity comes from the bus pointer /coord-bootstrap.json; the environment wins;
a hostile or malformed pointer yields nothing (the loud SKIPPED paths stay)."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "adopt-latest.sh"


def _block() -> str:
    text = SCRIPT.read_text()
    start = text.index("# ---- bootstrap-pointer fallback (BEGIN)")
    end = text.index("# ---- bootstrap-pointer fallback (END)")
    return text[start:end]


def _stub(tmp_path: Path, payload: str | None, rc: int = 0) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "fulcra-api"
    body = "#!/usr/bin/env bash\n"
    if payload is None:
        body += f"exit {rc}\n"
    else:
        body += 'if [ "$1" = file ] && [ "$2" = download ] && [ "$3" = /coord-bootstrap.json ]; then\n'
        body += f"  printf '%s' {payload!r} > \"$4\"; exit 0\nfi\nexit 1\n"
    stub.write_text(body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run(tmp_path: Path, bindir: Path, env_team: str = "", env_coord: str = "") -> tuple[str, str, str]:
    script = tmp_path / "run.sh"
    script.write_text(
        "set -u\n"
        f'TEAM="{env_team}"\nCOORD="{env_coord}"\n' + _block() + 'printf "TEAM=%s\\nCOORD=%s\\n" "$TEAM" "$COORD"\n'
    )
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if l.startswith(("TEAM=", "COORD=")))
    return lines.get("TEAM", ""), lines.get("COORD", ""), out.stdout


def test_pointer_fills_both_when_env_silent(tmp_path):
    team, coord, out = _run(tmp_path, _stub(tmp_path, '{"team": "acme", "coordinator": "coord-boss"}'))
    assert (team, coord) == ("acme", "coord-boss")
    assert "read from the bus pointer" in out


def test_environment_wins_over_pointer(tmp_path):
    team, coord, out = _run(tmp_path, _stub(tmp_path, '{"team": "acme", "coordinator": "coord-boss"}'), "envteam", "envcoord")
    assert (team, coord) == ("envteam", "envcoord")
    assert "read from the bus pointer" not in out


@pytest.mark.parametrize(
    "payload",
    [
        '{"team": "acme; rm -rf /", "coordinator": "x"}',
        '{"team": "acme\\nevil", "coordinator": "x"}',
        '{"team": "../escape", "coordinator": "x"}',
        '{"team": "-flag", "coordinator": "x"}',
        '{"team": "' + "a" * 65 + '", "coordinator": "x"}',
        '{"team": 42, "coordinator": "x"}',
        '["not", "a", "dict"]',
        "not json at all",
    ],
)
def test_hostile_or_malformed_pointer_yields_empty_team(tmp_path, payload):
    team, _coord, _out = _run(tmp_path, _stub(tmp_path, payload))
    assert team == ""


def test_download_failure_yields_empty_and_does_not_fail_the_run(tmp_path):
    team, coord, _out = _run(tmp_path, _stub(tmp_path, None, rc=1))
    assert (team, coord) == ("", "")


def test_no_client_yields_empty(tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    script = tmp_path / "run.sh"
    script.write_text("set -u\nTEAM=\"\"\nCOORD=\"\"\n" + _block() + 'printf "TEAM=%s\\n" "$TEAM"\n')
    bash = shutil.which("bash")
    assert bash
    out = subprocess.run([bash, str(script)], capture_output=True, text=True, env={"PATH": str(empty)}, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "TEAM=\n" in out.stdout
