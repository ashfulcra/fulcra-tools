"""The adoption-claim dedupe must never suppress a claim that was not sent.

codex-reviewer, 589 r1: the marker was uploaded after the send BLOCK regardless
of outcome. Shell being shell, the final `echo WARN` succeeds, so control
reached the upload — and the next wake then saw a marker for a claim that never
left the host. That is the opposite of the fail-open contract the block's own
comment states, and it is a false clear on the adoption record itself.

These run the REAL script fragment against stub binaries on PATH, because the
defect lives in shell control flow and no amount of reading it proves the
branch. Extracting the block keeps the test hermetic: the full script installs
packages.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"


def _claim_block() -> str:
    """The dedupe + send block, lifted verbatim from the shipped script."""
    text = SCRIPT.read_text()
    start = text.index('CLAIM_MARK="')
    # Walk to the END of the block by BALANCING if/fi rather than guessing an
    # anchor: this fragment nests three conditionals, and my first cut stopped
    # at the innermost `fi` and produced a syntax error the tests then reported
    # as a missing message. Counting is the only thing that survives an edit.
    depth, end = 0, None
    for i, line in enumerate(text[start:].splitlines(keepends=True)):
        stripped = line.strip()
        if stripped.startswith("if ") or stripped.startswith("if["):
            depth += 1
        elif stripped == "fi":
            depth -= 1
            if depth == 0:
                end = start + sum(
                    len(x) for x in text[start:].splitlines(keepends=True)[:i + 1])
                break
    assert end is not None, "unbalanced if/fi — extraction anchor moved"
    return text[start:end]


def _run(tmp_path, *, send_ok: bool, record_ok: bool):
    """Run the block with stubbed `coord-engine` / `fulcra-api` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "coord-engine").write_text(
        "#!/bin/sh\nexit %d\n" % (0 if send_ok else 1))
    (bin_dir / "fulcra-api").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  file) case "$2" in\n'
        '          stat) exit 1 ;;\n'                      # no marker yet
        f'          upload) echo "$4" >> "{tmp_path}/uploads.txt"; exit 0 ;;\n'  # $4 = DESTINATION

        '        esac ;;\n'
        "  record) exit %d ;;\n" % (0 if record_ok else 1) +
        "esac\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    script = f'set -u\nSLUG=adopted-x\nVER=v1\nA=agent\nTYPE=t\n{_claim_block()}'
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))
    uploads = (tmp_path / "uploads.txt")
    return proc, (uploads.read_text() if uploads.exists() else "")


def test_a_totally_failed_send_leaves_NO_marker(tmp_path):
    """Both paths fail. The next wake must retry, so nothing may be recorded."""
    proc, uploads = _run(tmp_path, send_ok=False, record_ok=False)
    assert "failed to send" in proc.stdout
    assert uploads == "", (
        "a claim that was never delivered must not suppress the next attempt; "
        f"marker(s) written: {uploads!r}")


def test_the_fallback_path_still_records(tmp_path):
    """The canonical send fails, the raw fallback succeeds — that IS delivery,
    so the marker is right and the next wake should stay quiet."""
    proc, uploads = _run(tmp_path, send_ok=False, record_ok=True)
    assert "RAW FALLBACK" in proc.stdout
    assert "adopted-x" in uploads


def test_the_happy_path_records(tmp_path):
    proc, uploads = _run(tmp_path, send_ok=True, record_ok=True)
    assert "adoption claim sent (tagged)" in proc.stdout
    assert "adopted-x" in uploads
