"""A failed adopt must never leave the host worse than it found it.

`fulcra-api` is the STORE CLIENT: every read and write on this bus goes through
it, so losing it takes the host off the bus entirely — and `coord-engine doctor`
then reports the adoption authority unreadable, aiming the next diagnosis at the
store rather than at the script that just broke it.

MEASURED, not theorised (coord-maintainer, 2026-08-10, macOS + uv 0.11.17): I ran
this script as the acceptance test for a pin I was about to publish. `uv tool
install --force fulcra-api` deletes the tool environment before reinstalling, the
delete failed with `Directory not empty (os error 66)` AFTER `bin/` was already
gone, and the host was left with a dangling shim, a directory `uv tool list`
calls malformed, and no executable. The leg is chained with `&&`, so the engine
never installed either, and both fallback installers failed for unrelated host
reasons. A host that had a working client before the run had none after it.

Not exotic: every macOS host on uv runs this leg on every NEW pin, because the
sentinel fast path only skips when the host is ALREADY at the pin being adopted,
which is never true at the moment a pin moves.

These drive the REAL function lifted from the shipped script against stub
binaries, because the defect lives in shell control flow — reading it proves
nothing, which is how it shipped.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"


def _store_client_fn() -> str:
    """`uv_store_client` plus the `try` helper it uses, verbatim from the script.

    Anchored on definition lines and closed by counting to the function's own
    `}` at column zero, so an edit inside the body cannot silently truncate the
    extraction the way a guessed end-anchor would.
    """
    text = SCRIPT.read_text()

    def _fn(name: str, opener: str) -> str:
        start = text.index(opener)
        rest = text[start:].splitlines(keepends=True)
        for i, line in enumerate(rest):
            if i and line.rstrip("\n") == "}":
                return "".join(rest[: i + 1])
        raise AssertionError(f"unterminated {name}() — extraction anchor moved")

    return (
        'ELOG="$(mktemp)"\nSTEP_FAILS=0\n'
        + _fn("try", "try() {")
        + "\n"
        + _fn("uv_store_client", "uv_store_client() {")
    )


def _run(tmp_path, *, client_works: bool, force_install_fails: bool,
         retry_fails: bool = False):
    """Run the real function with `fulcra-api` and `uv` stubbed on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv.log"

    # The store client: present-and-working, or absent entirely.
    (bin_dir / "fulcra-api").write_text(
        "#!/bin/sh\nexit 0\n" if client_works
        else "#!/bin/sh\necho 'not found' >&2\nexit 127\n")

    # `uv`: records every subcommand, and fails `tool install` on demand. The
    # retry is distinguished from the first attempt by a marker file, so one
    # stub can model "first force-install fails, the post-clean retry succeeds".
    (bin_dir / "uv").write_text(f"""#!/bin/sh
echo "$@" >> "{log}"
case "$1 $2" in
  "tool install")
      if [ -f "{tmp_path}/attempted" ]; then
        {"exit 1" if retry_fails else "exit 0"}
      fi
      touch "{tmp_path}/attempted"
      {"exit 1" if force_install_fails else "exit 0"} ;;
  "tool dir") echo "{tmp_path}/tools" ;;
  *) exit 0 ;;
esac
""")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (tmp_path / "tools" / "fulcra-api").mkdir(parents=True)

    proc = subprocess.run(
        ["bash", "-c", _store_client_fn() + '\nUV_BIN=uv\nuv_store_client\n'],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    return proc, log.read_text() if log.exists() else ""


def test_a_WORKING_client_is_never_force_reinstalled(tmp_path):
    """The bug, as a regression. A forced reinstall is what deletes the tool
    environment, so the only certain way not to destroy a working client is not
    to touch it destructively."""
    proc, uv_log = _run(tmp_path, client_works=True, force_install_fails=True)
    assert proc.returncode == 0, (
        f"a working client must not fail the leg: {proc.stderr}")
    assert "tool install --force fulcra-api" not in uv_log, (
        f"a working store client was force-reinstalled — that is the delete "
        f"that took a host off the bus: {uv_log!r}")


def test_a_working_client_is_still_UPGRADED(tmp_path):
    """Not-reinstalling must not become not-updating: the client still gets a
    non-destructive upgrade, whose failure the working copy survives."""
    _, uv_log = _run(tmp_path, client_works=True, force_install_fails=False)
    assert "tool upgrade fulcra-api" in uv_log, (
        f"the store client was never upgraded: {uv_log!r}")


def test_a_FAILED_upgrade_does_not_fail_the_leg(tmp_path):
    """The working copy is what matters. An upgrade that cannot complete leaves
    the host exactly as capable as it was, so the adopt continues."""
    class _AlwaysFail:
        pass
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    (bin_dir / "fulcra-api").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 1\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", _store_client_fn() + '\nUV_BIN=uv\nuv_store_client\n'],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"a failed UPGRADE of a still-working client aborted the adopt: "
        f"{proc.stderr}")


def test_an_ABSENT_client_is_installed(tmp_path):
    """Nothing to lose, so the forced install is correct here — this is the case
    the leg exists for, and the fix must not break it."""
    proc, uv_log = _run(tmp_path, client_works=False, force_install_fails=False)
    assert proc.returncode == 0
    assert "tool install --force fulcra-api" in uv_log


def test_a_HALF_REMOVED_tool_dir_is_cleared_and_retried_once(tmp_path):
    """The ENOTEMPTY case. uv aborts having already removed `bin/`, so the next
    attempt must clear the wreckage rather than cascade into fallbacks that
    cannot help. `uv tool uninstall` alone does not do it — the directory is
    exactly what failed to delete."""
    proc, uv_log = _run(tmp_path, client_works=False, force_install_fails=True)
    assert "tool uninstall fulcra-api" in uv_log, (
        f"the half-removed tool was never uninstalled: {uv_log!r}")
    assert not (tmp_path / "tools" / "fulcra-api").exists(), (
        "the half-removed tool DIRECTORY survived, so the retry hits the same "
        "delete failure")
    assert uv_log.count("tool install --force fulcra-api") == 2, (
        f"expected one retry after clearing, got: {uv_log!r}")
    assert proc.returncode == 0, "the retry succeeded but the leg still failed"


def test_a_retry_that_also_fails_reports_failure(tmp_path):
    """Self-healing must not become self-deceiving: when the client genuinely
    cannot be installed, the leg fails and the script's own ADOPT FAILED path
    runs. A rescue that reports success it did not achieve is the worse bug."""
    proc, _ = _run(tmp_path, client_works=False, force_install_fails=True,
                   retry_fails=True)
    assert proc.returncode != 0, (
        "an unrecoverable client install reported success")
