"""The WHOLE installer cascade must honour the client decision, not just one leg.

codex-reviewer, 597 r5. `uv_store_client` left a working client untouched and
then the pipx and pip fallbacks carried `fulcra-api` in their own package lists —
so an unrelated ENGINE install failure dropped through and force-reinstalled the
very client the helper had just promised not to touch. The preservation decision
was local to one leg; the destruction was reachable from two others.

That is the third time in this PR a rule landed in one place and its siblings
went without: the register projection vs the fan-out scan, then both readers vs
the zero-op carry, now one installer leg vs the cascade around it. Helper-scoped
tests cannot see it by construction — the bypass lives in the control flow
BETWEEN helpers — so these drive the real cascade end to end.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "adopt-latest.sh"


def _cascade() -> str:
    """The real classification + every installer leg, verbatim from the script.

    Spans `client_state()` through the pip fallback — the whole region in which
    a leg could reach `fulcra-api`. Bounded by locating both ends in the shipped
    text, so a bypass introduced anywhere between them is inside what runs here.
    """
    text = SCRIPT.read_text()
    start = text.index("client_state() {")
    end_anchor = 'if [ -z "$INSTALLER" ]; then\n  # Same rule:'
    end = text.index(end_anchor)
    # close out the pip if/else/fi block that follows the anchor
    tail = text[end:]
    end += tail.index("\nfi\n") + len("\nfi\n")
    body = text[start:end]
    assert "pipx install --force fulcra-api" in body, "pipx leg fell outside the slice"
    assert "pip user-install" in body, "pip leg fell outside the slice"
    return (
        'ELOG="$(mktemp)"\nSTEP_FAILS=0\nINSTALLER=""\n'
        'SRC="git+engine"\nCOMMON="git+common"\nUV_BIN=uv\n'
        + _fn(text, "try() {")
        + "\n"
        + body
        + '\necho "INSTALLER=${INSTALLER}"\n'
    )


def _fn(text: str, opener: str) -> str:
    start = text.index(opener)
    rest = text[start:].splitlines(keepends=True)
    for i, line in enumerate(rest):
        if i and line.rstrip("\n") == "}":
            return "".join(rest[: i + 1])
    raise AssertionError(f"unterminated {opener} — extraction anchor moved")


def _run_cascade(tmp_path, *, client_works: bool, uv_engine_fails: bool,
                 have_pipx: bool = True):
    """Run the real cascade with uv/pipx/pip stubbed and every call recorded.

    The stubs are DESTRUCTIVE on a forced client install — they delete the
    client, as the real ones can — so preservation is asserted against the file
    on disk rather than against a log line.
    """
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"
    client = bin_dir / "fulcra-api"
    if client_works:
        client.write_text("#!/bin/sh\nexit 0\n")

    (bin_dir / "uv").write_text(f"""#!/bin/sh
echo "uv $@" >> "{log}"
case "$*" in
  *"tool install --force fulcra-api"*) rm -f "{client}" ;;
  *"tool install --force git+engine"*) {"exit 1" if uv_engine_fails else "exit 0"} ;;
esac
case "$1 $2" in "tool dir") echo "{tmp_path}/tools" ;; esac
exit 0
""")
    if have_pipx:
        (bin_dir / "pipx").write_text(f"""#!/bin/sh
echo "pipx $@" >> "{log}"
case "$*" in *"install --force fulcra-api"*) rm -f "{client}" ;; esac
exit 0
""")
    (bin_dir / "python3").write_text(f"""#!/bin/sh
echo "python3 $@" >> "{log}"
case "$*" in *" fulcra-api "*|*" fulcra-api") rm -f "{client}" ;; esac
exit 0
""")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    proc = subprocess.run(
        ["bash", "-c", _cascade()],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    return proc, (log.read_text() if log.exists() else ""), client


def test_an_ENGINE_failure_cannot_reach_the_working_client_via_pipx(tmp_path):
    """codex's exact reproduction. The uv engine install fails, `INSTALLER`
    stays empty, and the pipx fallback runs — carrying `fulcra-api` with it."""
    proc, calls, client = _run_cascade(
        tmp_path, client_works=True, uv_engine_fails=True)
    assert client.exists(), (
        f"an unrelated ENGINE failure destroyed the working store client "
        f"through a fallback leg. calls:\n{calls}")
    assert "install --force fulcra-api" not in calls, (
        f"a fallback force-installed the client the uv leg had preserved:\n{calls}")


def test_the_pip_fallback_does_not_UPGRADE_a_working_client(tmp_path):
    """The pip leg passed `--upgrade fulcra-api`, which mutates a working client
    — the thing r3 established this script must not do unattended. It must
    install the engine and common dependency only."""
    _, calls, client = _run_cascade(
        tmp_path, client_works=True, uv_engine_fails=True, have_pipx=False)
    assert client.exists(), f"the pip fallback removed the client:\n{calls}"
    pip_calls = [c for c in calls.splitlines() if c.startswith("python3")]
    assert pip_calls, f"the pip leg never ran, so this proves nothing:\n{calls}"
    # Narrowed to INSTALL invocations on purpose. The assertion is about what
    # the pip leg puts in its package list, and the script also shells out to
    # `python3 -c "importlib.metadata.version('fulcra-api')"` to READ the
    # installed version for the floor check. That read names the client without
    # touching it, so matching every python3 call flagged a read as a mutation.
    # Do not re-broaden this: "mentions fulcra-api" is not the property under
    # test, "installs or upgrades fulcra-api" is.
    installs = [c for c in pip_calls if "-m pip" in c]
    assert installs, f"the pip INSTALL leg never ran, so this proves nothing:\n{calls}"
    assert not any("fulcra-api" in c for c in installs), (
        f"the pip fallback still carried the client in its package list: {installs}")


def test_an_ABSENT_client_is_still_installed_by_a_fallback(tmp_path):
    """The rule must not become "never install the client": when there is
    nothing to lose, a fallback is exactly what should recover the host."""
    _, calls, _ = _run_cascade(
        tmp_path, client_works=False, uv_engine_fails=True)
    assert "install --force fulcra-api" in calls, (
        f"an absent client was not installed by any leg, so a host with no "
        f"client can never recover:\n{calls}")


def test_the_engine_is_still_installed_when_the_client_is_preserved(tmp_path):
    """Preserving the client must not stop the cascade doing its actual job."""
    proc, calls, client = _run_cascade(
        tmp_path, client_works=True, uv_engine_fails=False)
    assert client.exists()
    assert "git+engine" in calls, f"the engine was never installed:\n{calls}"
    assert "INSTALLER=uv" in proc.stdout, (
        f"the cascade did not record a successful installer: {proc.stdout!r}")
