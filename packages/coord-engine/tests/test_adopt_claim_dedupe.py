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

    # `set -u` is not decoration: it is line 14 of the shipped script, and the
    # r2 defect existed ONLY under it. The trailing sentinel is what lets a
    # test distinguish "said the right thing and finished" from "said the right
    # thing on its way out the door" — the first cut of these tests asserted
    # only on stdout and the upload log, both of which are already true at the
    # moment the shell dies, so they passed on a script that had crashed
    # (coord-opus-worker, 589 r2).
    # TEAM/COORD/WHO model what the shipped script resolves from the
    # environment before this block runs. They are preconditions, not
    # decoration: under `set -u` an undefined one kills the fragment at line 1
    # and every assertion below then reports the wrong cause.
    script = (f'set -u\nSLUG=adopted-x\nVER=v1\nA=agent\nTYPE=t\n'
              f'TEAM=acme\nCOORD=team-coordinator\nWHO=team-coordinator\n'
              f'{_claim_block()}\necho BLOCK-COMPLETED\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))
    uploads = (tmp_path / "uploads.txt")
    return proc, (uploads.read_text() if uploads.exists() else "")


def _assert_ran_to_completion(proc):
    """The block must FINISH, not merely print the right line before dying.

    Exit status is load-bearing here: the caller's rc is what the wake protocol
    reports, and a `set -u` death replaces a DEGRADED rc 3 with a generic 1.
    """
    assert "BLOCK-COMPLETED" in proc.stdout, (
        "the block did not run to completion — it exited early. stderr:\n"
        f"{proc.stderr}")
    assert proc.returncode == 0, (
        f"non-zero exit ({proc.returncode}) destroys the caller's rc. stderr:\n"
        f"{proc.stderr}")


def test_a_totally_failed_send_leaves_NO_marker(tmp_path):
    """Both paths fail. The next wake must retry, so nothing may be recorded."""
    proc, uploads = _run(tmp_path, send_ok=False, record_ok=False)
    assert "failed to send" in proc.stdout
    assert uploads == "", (
        "a claim that was never delivered must not suppress the next attempt; "
        f"marker(s) written: {uploads!r}")
    # This is the path where `_CM` is never assigned, so it is the one that
    # dies under `set -u` if the cleanup ever drifts back outside the branch.
    _assert_ran_to_completion(proc)


def test_the_fallback_path_still_records(tmp_path):
    """The canonical send fails, the raw fallback succeeds — that IS delivery,
    so the marker is right and the next wake should stay quiet."""
    proc, uploads = _run(tmp_path, send_ok=False, record_ok=True)
    assert "RAW FALLBACK" in proc.stdout
    assert "adopted-x" in uploads
    _assert_ran_to_completion(proc)


def test_the_happy_path_records(tmp_path):
    proc, uploads = _run(tmp_path, send_ok=True, record_ok=True)
    assert "adoption claim sent (tagged)" in proc.stdout
    assert "adopted-x" in uploads
    _assert_ran_to_completion(proc)


def _guarded_claim_block() -> str:
    """The claim block INCLUDING its team/coordinator guard.

    `_claim_block` deliberately starts at `CLAIM_MARK=` and so extracts only
    the inside. The guard is the entire point of the branch below, so it gets
    its own extraction rather than a copy of the condition living in the test.
    """
    text = SCRIPT.read_text()
    start = text.index('if [ -n "$TEAM" ] && [ -n "$COORD" ]; then')
    depth, end = 0, None
    seg = text[start:].splitlines(keepends=True)
    for i, line in enumerate(seg):
        stripped = line.strip()
        if stripped.startswith("if ") or stripped.startswith("if["):
            depth += 1
        elif stripped == "fi":
            depth -= 1
            if depth == 0:
                end = start + sum(len(x) for x in seg[:i + 1])
                break
    assert end is not None, "unbalanced if/fi — guard extraction anchor moved"
    return text[start:end]


def test_an_unconfigured_host_skips_the_claim_instead_of_filing_it_nowhere(tmp_path):
    """No team and no coordinator means the claim has no path and no addressee.

    The failure being guarded is not the skip, it is the MARKER: an empty TEAM
    builds "team//_coord/bus-v3/adopted/<slug>.txt", and a marker written there
    would suppress the retry once the variables are finally set. The claim
    would then be owed forever with nothing left to say so.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "coord-engine").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "fulcra-api").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  file) case "$2" in\n'
        '          stat) exit 1 ;;\n'
        f'          upload) echo "$4" >> "{tmp_path}/uploads.txt"; exit 0 ;;\n'
        '        esac ;;\n'
        "  record) exit 0 ;;\n"
        "esac\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    script = (
        'set -u\nSLUG=adopted-x\nVER=v1\nA=agent\nTYPE=t\n'
        'TEAM=\nCOORD=\nWHO="your coordinator"\n'
        f'{_guarded_claim_block()}\necho BLOCK-COMPLETED\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))

    _assert_ran_to_completion(proc)
    assert "claim SKIPPED" in proc.stderr, proc.stderr
    uploads = tmp_path / "uploads.txt"
    assert not uploads.exists(), (
        "a host with no team must not write a marker; it wrote: "
        f"{uploads.read_text()!r}")
