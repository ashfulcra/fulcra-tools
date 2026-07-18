"""CI tests for the fulcra-agent-automation bundled installers.

These bash scripts write launchd jobs / crontab lines — the repo's
highest-risk executables (see docs/skill-quality-pattern.md). Strategy: run
them for real in a subprocess with a throwaway HOME and a PATH shim dir whose
fake `launchctl`, `plutil`, `osascript`, `crontab`, `uname` (parametrized
Darwin/Linux so BOTH platform branches run on any CI host) and `coord-engine`
record their invocations. The crontab shim persists to a file with real
`-l` / stdin-replace semantics so dedup and uninstall are exercised honestly.
"""

import json
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "fulcra-agent-automation" / "scripts"

SHIMS = {
    "launchctl": '#!/bin/sh\necho "launchctl $*" >> "${SHIM_LOG:-/dev/null}"\nexit 0\n',
    "plutil": '#!/bin/sh\necho "plutil $*" >> "${SHIM_LOG:-/dev/null}"\nexit 0\n',
    "osascript": '#!/bin/sh\necho "osascript" >> "${SHIM_LOG:-/dev/null}"\nexit 0\n',
    "coord-engine": '#!/bin/sh\necho "coord-engine $*" >> "${SHIM_LOG:-/dev/null}"\nexit 0\n',
    "fulcra-api": '#!/bin/sh\nexit 0\n',
    # Stands in for the interpreter of coord-engine's venv: the heartbeat
    # installer's self-test runs `<dirname of coord-engine>/python -c "import
    # fulcra_common"` UNCONDITIONALLY (the chain always emits timeline moments
    # via `digest --emit-timeline`). Exit 0 = writer importable. Tests for the
    # writer-absent contract delete this shim.
    "python": '#!/bin/sh\nexit 0\n',
    # real-semantics crontab: -l prints the store (exit 1 if absent, like real
    # cron), bare invocation replaces the store from stdin
    "crontab": (
        '#!/bin/sh\n'
        'if [ "$1" = "-l" ]; then\n'
        '  [ -f "$CRON_STORE" ] || exit 1\n'
        '  cat "$CRON_STORE"\n'
        'else\n'
        '  cat > "$CRON_STORE"\n'
        'fi\n'
    ),
}


def _mkenv(tmp_path, platform):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "shim.log"
    log.write_text("")
    cron_store = tmp_path / "cron.store"
    for name, body in {**SHIMS, "uname": f'#!/bin/sh\necho {platform}\n'}.items():
        f = shims / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    e = {"HOME": str(home), "PATH": f"{shims}:/usr/bin:/bin",
         "SHIM_LOG": str(log), "CRON_STORE": str(cron_store),
         "TERM": "dumb", "LANG": os.environ.get("LANG", "C")}
    return {"env": e, "home": home, "log": log, "cron": cron_store,
            "shims": shims, "platform": platform}


@pytest.fixture(params=["Darwin", "Linux"])
def env(tmp_path, request):
    return _mkenv(tmp_path, request.param)


@pytest.fixture()
def mac(tmp_path):
    return _mkenv(tmp_path, "Darwin")


def _run(script, args, env):
    return subprocess.run(["bash", str(SCRIPTS / script), *args],
                          capture_output=True, text=True, env=env["env"], timeout=60)


def _plists(env):
    return sorted((env["home"] / "Library" / "LaunchAgents").glob("*.plist"))


def _cron_lines(env):
    if not env["cron"].exists():
        return []
    return [l for l in env["cron"].read_text().splitlines() if l.strip()]


class TestHeartbeatBothPlatforms:
    def test_install_creates_exactly_one_job(self, env):
        r = _run("install-heartbeat.sh", ["--yes", "teamx", "15"], env)
        assert r.returncode == 0, r.stderr
        if env["platform"] == "Darwin":
            plists = _plists(env)
            assert [p.name for p in plists] == ["com.fulcra.coord-engine.heartbeat.teamx.plist"]
            pl = plistlib.loads(plists[0].read_bytes())
            assert pl["Label"] == "com.fulcra.coord-engine.heartbeat.teamx"  # label == filename stem
            assert pl["StartInterval"] == 15 * 60
            args = [str(a) for a in pl["ProgramArguments"]]
            # heartbeat chains reconcile + `annotate project` via /bin/sh -c so a
            # team opted into projection lands its transitions on the timeline.
            assert args[:2] == ["/bin/sh", "-c"]
            assert "reconcile teamx" in args[-1] and "annotate project teamx" in args[-1]
            # the hardening the script exists for: pinned HOME + shims-first PATH
            envd = pl["EnvironmentVariables"]
            assert envd["HOME"] == str(env["home"])
            assert envd["PATH"].startswith(str(env["shims"]))
            log = env["log"].read_text()
            assert "launchctl load" in log and "plutil -lint" in log
        else:
            lines = _cron_lines(env)
            assert len(lines) == 1
            assert "reconcile teamx" in lines[0] and "annotate project teamx" in lines[0]
            assert lines[0].startswith("*/15 ")
            assert "# com.fulcra.coord-engine.heartbeat.teamx" in lines[0]

    def test_reinstall_is_idempotent(self, env):
        assert _run("install-heartbeat.sh", ["--yes", "teamx"], env).returncode == 0
        assert _run("install-heartbeat.sh", ["--yes", "teamx"], env).returncode == 0
        if env["platform"] == "Darwin":
            assert len(_plists(env)) == 1
        else:
            assert len(_cron_lines(env)) == 1  # dedup by label comment, never accumulates

    def test_selftest_verifies_timeline_writer_present(self, env):
        # The chain requests timeline emission unconditionally, so a successful
        # install must have PROVEN the writer importable — not just chain rc 0.
        r = _run("install-heartbeat.sh", ["--yes", "teamx"], env)
        assert r.returncode == 0, r.stderr
        assert "timeline writer present" in r.stdout

    def test_selftest_fails_loud_when_writer_absent(self, env):
        # codex docs-QA P1 (round 2): a missing fulcra_common writer means the
        # digest/projection emits silently no-op — the dark-timeline condition.
        # The self-test must FAIL the install regardless of the team's
        # projection resolution (the digest leg runs at any resolution), with
        # the reinstall recipe on stderr. Writer absence is simulated by
        # removing the venv-interpreter shim next to coord-engine.
        (env["shims"] / "python").unlink()
        r = _run("install-heartbeat.sh", ["--yes", "teamx"], env)
        assert r.returncode == 4, (r.returncode, r.stderr)
        assert "self-test FAILED" in r.stderr
        assert "fulcra-common-v0.2.0" in r.stderr, "must print the reinstall recipe"

    def test_uninstall_roundtrip_including_only_entry(self, env):
        # the pipefail bug class: uninstalling when ours is the ONLY entry must exit 0
        assert _run("install-heartbeat.sh", ["--yes", "teamx"], env).returncode == 0
        env["log"].write_text("")
        r = _run("install-heartbeat.sh", ["--uninstall", "teamx"], env)
        assert r.returncode == 0, r.stderr
        assert "uninstalled" in r.stdout
        if env["platform"] == "Darwin":
            assert _plists(env) == []
            assert "launchctl unload" in env["log"].read_text()
        else:
            assert _cron_lines(env) == []

    def test_uninstall_preserves_foreign_cron_lines(self, env):
        if env["platform"] == "Darwin":
            pytest.skip("cron-only behavior")
        env["cron"].write_text("0 1 * * * /usr/bin/foreign-job\n")
        assert _run("install-heartbeat.sh", ["--yes", "teamx"], env).returncode == 0
        assert len(_cron_lines(env)) == 2
        assert _run("install-heartbeat.sh", ["--uninstall", "teamx"], env).returncode == 0
        assert _cron_lines(env) == ["0 1 * * * /usr/bin/foreign-job"]

    def test_rejects_bad_team_name(self, env):
        r = _run("install-heartbeat.sh", ["--yes", "bad/../team"], env)
        assert r.returncode != 0
        assert _plists(env) == [] and _cron_lines(env) == []


class TestListenerBothPlatforms:
    ARGS = ["--yes", "teamx", "coord-maintainer", "10"]

    def test_install_creates_exactly_one_job(self, env):
        r = _run("install-listener.sh", self.ARGS, env)
        assert r.returncode == 0, r.stderr
        if env["platform"] == "Darwin":
            plists = _plists(env)
            assert len(plists) == 1, [p.name for p in plists]
            # filename pins the SKILL.md probe contract: sanitized agent + checksum suffix
            assert re.fullmatch(
                r"com\.fulcra\.coord-engine\.listener\.teamx\.coord-maintainer-\d+\.plist",
                plists[0].name), plists[0].name
            pl = plistlib.loads(plists[0].read_bytes())
            assert pl["Label"] == plists[0].name[:-len(".plist")]
            assert pl["StartInterval"] == 10 * 60
            assert any("listener-tick" in str(a) for a in pl["ProgramArguments"])
            assert "teamx" in pl["ProgramArguments"]           # from codex-reviewer's pin
            assert "coord-maintainer" in pl["ProgramArguments"]
            envd = pl["EnvironmentVariables"]
            assert envd["HOME"] == str(env["home"])
            assert envd["PATH"].startswith(str(env["shims"]))
            log = env["log"].read_text()
            assert "launchctl load" in log and "plutil -lint" in log
        else:
            lines = _cron_lines(env)
            assert len(lines) == 1 and "listener-tick" in lines[0]

    def test_uninstall_roundtrip(self, env):
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        env["log"].write_text("")
        r = _run("install-listener.sh", ["--uninstall", "teamx", "coord-maintainer"], env)
        assert r.returncode == 0, r.stderr
        assert _plists(env) == [] and _cron_lines(env) == []
        if env["platform"] == "Darwin":
            assert "launchctl unload" in env["log"].read_text()

    def test_reinstall_is_idempotent(self, env):
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        if env["platform"] == "Darwin":
            assert len(_plists(env)) == 1
        else:
            assert len(_cron_lines(env)) == 1


class TestListenerTick:
    def _run_tick(self, tmp_path, once_stdout="", once_stderr="", *, verbose=False):
        shims = tmp_path / "shims"
        shims.mkdir()
        engine = shims / "coord-engine"
        engine.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *' --state-path '*) printf '%s\\n' \"$COORD_TEST_STATE\" ;;\n"
            "  *) printf '%s' \"$COORD_TEST_STDOUT\"; "
            "printf '%s' \"$COORD_TEST_STDERR\" >&2 ;;\n"
            "esac\n")
        engine.chmod(0o755)
        env = {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{shims}:/usr/bin:/bin",
            "COORD_LISTENER_STATE": str(tmp_path / "state"),
            "COORD_TEST_STATE": str(tmp_path / "state" / "listen.json"),
            "COORD_TEST_STDOUT": once_stdout,
            "COORD_TEST_STDERR": once_stderr,
            "COORD_LISTENER_VERBOSE": "1" if verbose else "0",
        }
        return subprocess.run(
            ["bash", str(SCRIPTS / "listener-tick.sh"), "teamx", "agent"],
            capture_output=True, text=True, env=env, timeout=20)

    def test_quiet_tick_is_silent_by_default(self, tmp_path):
        r = self._run_tick(tmp_path)
        assert r.returncode == 0
        assert r.stdout == "" and r.stderr == ""

    def test_quiet_tick_can_be_verbose(self, tmp_path):
        r = self._run_tick(tmp_path, verbose=True)
        assert r.returncode == 0
        assert "no new events" in r.stdout

    def test_degradation_is_fail_visible(self, tmp_path):
        r = self._run_tick(tmp_path, once_stderr="LISTEN DEGRADED: inbox unreadable\n")
        assert r.returncode == 0
        assert "LISTEN DEGRADED: inbox unreadable" in r.stderr
        assert "listener degraded" in r.stdout


class TestOpenClawWakeAdapter:
    def test_posts_fixed_authenticated_wake(self, tmp_path):
        shims = tmp_path / "shims"
        shims.mkdir()
        curl = shims / "curl"
        capture = tmp_path / "curl-args"
        curl.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$@" > "$CURL_CAPTURE"\n')
        curl.chmod(0o755)
        env = {
            "PATH": f"{shims}:/usr/bin:/bin",
            "CURL_CAPTURE": str(capture),
            "OPENCLAW_HOOK_TOKEN": "secret-token",
            "COORD_LISTENER_TEAM": "teamx",
            "COORD_LISTENER_AGENT": "agent",
            "COORD_LISTENER_DEGRADED": "1",
        }
        r = subprocess.run(
            ["bash", str(SCRIPTS / "wake" / "openclaw.sh")],
            capture_output=True, text=True, env=env, timeout=20)
        assert r.returncode == 0, r.stderr
        args = capture.read_text().splitlines()
        assert "Authorization: Bearer secret-token" in args
        assert "http://127.0.0.1:18789/hooks/wake" in args
        payload = json.loads(args[args.index("--data-binary") + 1])
        assert payload["mode"] == "now"
        assert "teamx" in payload["text"] and "agent" in payload["text"]
        assert "targeted fallback" in payload["text"]

    def test_requires_token_before_network(self, tmp_path):
        r = subprocess.run(
            ["bash", str(SCRIPTS / "wake" / "openclaw.sh")],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=20)
        assert r.returncode != 0
        assert "set OPENCLAW_HOOK_TOKEN or create" in r.stderr

    def test_reads_private_scheduler_token_file(self, tmp_path):
        shims = tmp_path / "shims"
        shims.mkdir()
        curl = shims / "curl"
        capture = tmp_path / "curl-args"
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_CAPTURE"\n')
        curl.chmod(0o755)
        token = tmp_path / ".config" / "coord-engine" / "openclaw-hook-token"
        token.parent.mkdir(parents=True)
        token.write_text("file-secret\n")
        token.chmod(0o600)
        r = subprocess.run(
            ["bash", str(SCRIPTS / "wake" / "openclaw.sh")],
            capture_output=True, text=True,
            env={"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path),
                 "CURL_CAPTURE": str(capture)}, timeout=20)
        assert r.returncode == 0, r.stderr
        assert "Authorization: Bearer file-secret" in capture.read_text()

    def test_rejects_group_readable_token_file(self, tmp_path):
        token = tmp_path / "token"
        token.write_text("not-private")
        token.chmod(0o640)
        r = subprocess.run(
            ["bash", str(SCRIPTS / "wake" / "openclaw.sh")],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                 "OPENCLAW_HOOK_TOKEN_FILE": str(token)}, timeout=20)
        assert r.returncode == 2
        assert "mode 0600 or 0400" in r.stderr


class TestWakeCmdThreatModel:
    """The guards reject exactly what could break out of the two embeddings:
    launchd puts WAKE_CMD in an XML <string> (breakout needs '<', rejected; '&'
    is escaped) as /bin/sh -c argv; cron wraps it in single quotes on one line
    (breakout needs a quote or newline, both rejected). Backticks/$()/backslash
    are DELIBERATELY allowed — they only run inside the consented /bin/sh -c
    payload, which is the feature the operator approved at install time."""

    def test_rejected_metacharacters(self, mac):
        for evil in ["do'evil", "a<b", "line1\nline2"]:
            r = _run("install-listener.sh",
                     ["--yes", "teamx", "agent", "10", "--wake-cmd", evil], mac)
            assert r.returncode != 0, f"accepted: {evil!r}"
        assert _plists(mac) == []

    def test_accepted_ampersand_lands_escaped_and_intact(self, mac):
        cmd = "notify --to a&b $(hostname)"
        r = _run("install-listener.sh",
                 ["--yes", "teamx", "agent", "10", "--wake-cmd", cmd], mac)
        assert r.returncode == 0, r.stderr
        pl = plistlib.loads(_plists(mac)[0].read_bytes())
        args = [str(a) for a in pl["ProgramArguments"]]
        # plistlib decodes &amp; back to & — the escaping round-trips, cmd intact
        assert args[-1] == cmd and args[-3:-1] == ["/bin/sh", "-c"]


class TestFailurePaths:
    def test_plutil_lint_failure_removes_plist_and_fails(self, mac):
        bad = mac["shims"] / "plutil"
        bad.write_text('#!/bin/sh\nexit 1\n')
        r = _run("install-heartbeat.sh", ["--yes", "teamx"], mac)
        assert r.returncode == 4
        assert _plists(mac) == []  # never leave garbage a future load could pick up
