"""The store-client version floor in adopt-latest.sh.

WHY THIS EXISTS AS A TEST AND NOT A COMMENT. The classifier in that script
calls a client "working" when `fulcra-api --help` answers, and that probe is
structurally blind to the failure the floor guards: the client starts fine,
prints help fine, and dies on the first command that loads credentials —

    TypeError: FulcraCredentials.__init__() got an unexpected keyword
               argument 'id_token'

Measured 2026-09-05 across every published release: 0.1.34 through 0.1.39 all
reject a credentials document carrying `id_token`; 0.1.40 accepts. So the floor
is an exact measurement, and a gate whose OFF state looks identical to its ON
state needs a test that drives both.

These run the real fragment against a stub `uv` on PATH, the same way
test_adopt_claim_dedupe runs the real claim block: the logic is shell control
flow, and reading it proves nothing about what it does.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"


def _floor_block() -> str:
    """Floor constant, helpers and classification, lifted from the script."""
    text = SCRIPT.read_text()
    start = text.index("FULCRA_API_FLOOR=")
    end = text.index("uv_store_client() {")
    return text[start:end]


def _classify(tmp_path, version: str | None, *, uv_on_path: bool = True):
    """Run the real block with a stub `uv` reporting `version`.

    `uv_on_path=False` models a lean launchd/cron PATH: uv exists only at the
    absolute location this script resolves into UV_BIN, and is NOT findable by
    name. That is the case the floor probe has to survive.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if version is None:
        (bin_dir / "uv").write_text("#!/bin/sh\nexit 1\n")
    else:
        (bin_dir / "uv").write_text(
            '#!/bin/sh\n'
            '[ "$1" = "tool" ] && [ "$2" = "list" ] && echo "fulcra-api v%s"\n'
            'exit 0\n' % version)
    # `--help` answers, so the existing classifier calls this client "working".
    (bin_dir / "fulcra-api").write_text("#!/bin/sh\nexit 0\n")
    # Force the uv path so the metadata fallback is not what is under test.
    (bin_dir / "python3").write_text("#!/bin/sh\nexit 1\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    if uv_on_path:
        uv_bin, path = "", f"{bin_dir}:/usr/bin:/bin"
    else:
        # uv is moved somewhere PATH cannot reach, and only UV_BIN knows it.
        hidden = tmp_path / "opt"
        hidden.mkdir()
        (bin_dir / "uv").rename(hidden / "uv")
        uv_bin, path = f'UV_BIN="{hidden / "uv"}"\n', f"{bin_dir}:/usr/bin:/bin"

    script = (f'set -u\nCLIENT_STATE=working\n{uv_bin}{_floor_block()}'
              f'\necho "STATE=$CLIENT_STATE"\n')
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": path, "HOME": str(tmp_path)})
    assert "STATE=" in proc.stdout, f"fragment did not finish:\n{proc.stderr}"
    return proc.stdout.strip().split("STATE=")[1].strip(), proc.stderr


@pytest.mark.parametrize("version", ["0.1.34", "0.1.35", "0.1.39", "0.1.9"])
def test_a_client_below_the_floor_is_stale_however_well_it_answers(tmp_path, version):
    """`--help` answering is exactly what makes this dangerous."""
    state, err = _classify(tmp_path, version)
    assert state == "stale", err
    assert "BELOW the" in err


@pytest.mark.parametrize("version", ["0.1.40", "0.1.41", "0.2.0", "1.0.0"])
def test_a_client_at_or_above_the_floor_is_left_alone(tmp_path, version):
    state, err = _classify(tmp_path, version)
    assert state == "working", err
    assert "BELOW the" not in err


def test_0_1_9_is_below_0_1_40_which_a_string_compare_gets_backwards(tmp_path):
    """The comparison is numeric per component, not lexical.

    Lexically "0.1.9" sorts AFTER "0.1.40", so a string compare would call the
    oldest client on the fleet current and skip it silently — the one outcome
    this gate must never produce.
    """
    state, _ = _classify(tmp_path, "0.1.9")
    assert state == "stale"


def test_an_unreadable_version_is_UNKNOWN_and_never_treated_as_below(tmp_path):
    """Refusing to enforce a floor you cannot measure is the same discipline as
    refusing to delete something you cannot classify. UNKNOWN must not trigger
    an upgrade of a client that is answering."""
    state, err = _classify(tmp_path, None)
    assert state == "working", err
    assert "NOT enforced" in err


def test_the_floor_is_stated_once_and_the_tests_read_it_from_the_script(tmp_path):
    """A floor duplicated into the test is a floor that can silently disagree."""
    block = _floor_block()
    assert 'FULCRA_API_FLOOR="0.1.40"' in block
    state, _ = _classify(tmp_path, "0.1.40")
    assert state == "working"


def test_a_lean_PATH_still_sees_an_old_tool_through_UV_BIN(tmp_path):
    """codex-reviewer P1 on PR 706.

    This script resolves UV_BIN beyond PATH precisely because launchd, cron and
    systemd run lean PATHs where "uv not found" is falsely true. A floor probe
    calling bare `uv` misses the installer there, falls through to unrelated
    python3 metadata, reports UNKNOWN — and UNKNOWN deliberately does not
    upgrade. So the gate would silently skip exactly the hosts least likely to
    be fixed by hand, while looking like a considered decision.
    """
    state, err = _classify(tmp_path, "0.1.35", uv_on_path=False)
    assert state == "stale", (
        "a lean PATH hid the old client from the floor probe:\n" + err)
    assert "BELOW the" in err


def test_a_lean_PATH_does_not_invent_a_version_for_a_current_tool(tmp_path):
    """The same path must not flip the other way and upgrade something current."""
    state, err = _classify(tmp_path, "0.1.40", uv_on_path=False)
    assert state == "working", err
