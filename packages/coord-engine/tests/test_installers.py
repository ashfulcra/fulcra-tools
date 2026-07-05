"""CI tests for the fulcra-agent-automation bundled installers.

These bash scripts write launchd jobs — the repo's highest-risk executables
(see docs/skill-quality-pattern.md). Strategy: run them for real in a
subprocess with a throwaway HOME and a PATH shim dir whose fake `launchctl`,
`plutil`, `osascript`, `uname` (pinned to Darwin so the macOS path runs on any
CI host) and `coord-engine` record their invocations to a log file.
"""

import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "fulcra-agent-automation" / "scripts"

SHIMS = {
    "uname": '#!/bin/sh\necho Darwin\n',
    "launchctl": '#!/bin/sh\necho "launchctl $*" >> "$SHIM_LOG"\nexit 0\n',
    "plutil": '#!/bin/sh\necho "plutil $*" >> "$SHIM_LOG"\nexit 0\n',
    "osascript": '#!/bin/sh\necho "osascript" >> "$SHIM_LOG"\nexit 0\n',
    "coord-engine": '#!/bin/sh\necho "coord-engine $*" >> "$SHIM_LOG"\nexit 0\n',
    "fulcra-api": '#!/bin/sh\nexit 0\n',
}


@pytest.fixture()
def env(tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "shim.log"
    log.write_text("")
    for name, body in SHIMS.items():
        f = shims / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    e = dict(os.environ)
    e.update({"HOME": str(home), "PATH": f"{shims}:/usr/bin:/bin",
              "SHIM_LOG": str(log)})
    return {"env": e, "home": home, "log": log}


def _run(script, args, env):
    return subprocess.run(["bash", str(SCRIPTS / script), *args],
                          capture_output=True, text=True, env=env["env"], timeout=60)


def _plists(env):
    return sorted((env["home"] / "Library" / "LaunchAgents").glob("*.plist"))


class TestHeartbeat:
    def test_install_writes_valid_plist_and_loads(self, env):
        r = _run("install-heartbeat.sh", ["--yes", "teamx", "15"], env)
        assert r.returncode == 0, r.stderr
        plists = _plists(env)
        assert len(plists) == 1
        pl = plistlib.loads(plists[0].read_bytes())
        assert pl["StartInterval"] == 15 * 60
        joined = " ".join(str(a) for a in pl["ProgramArguments"])
        assert "reconcile" in joined and "teamx" in joined
        log = env["log"].read_text()
        assert "launchctl load" in log
        assert "plutil -lint" in log

    def test_uninstall_removes_and_unloads(self, env):
        assert _run("install-heartbeat.sh", ["--yes", "teamx"], env).returncode == 0
        env["log"].write_text("")
        r = _run("install-heartbeat.sh", ["--uninstall", "teamx"], env)
        assert r.returncode == 0, r.stderr
        assert _plists(env) == []
        assert "launchctl unload" in env["log"].read_text()

    def test_rejects_bad_team_name(self, env):
        r = _run("install-heartbeat.sh", ["--yes", "bad/../team"], env)
        assert r.returncode != 0
        assert _plists(env) == []


class TestListener:
    ARGS = ["--yes", "teamx", "coord-maintainer", "10"]

    def test_install_writes_valid_plist_and_loads(self, env):
        r = _run("install-listener.sh", self.ARGS, env)
        assert r.returncode == 0, r.stderr
        plists = _plists(env)
        assert len(plists) == 1, [p.name for p in plists]
        pl = plistlib.loads(plists[0].read_bytes())
        assert pl["StartInterval"] == 10 * 60
        joined = " ".join(str(a) for a in pl["ProgramArguments"])
        assert "listener-tick" in joined
        log = env["log"].read_text()
        assert "launchctl load" in log and "plutil -lint" in log

    def test_uninstall_roundtrip(self, env):
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        env["log"].write_text("")
        r = _run("install-listener.sh", ["--uninstall", "teamx", "coord-maintainer"], env)
        assert r.returncode == 0, r.stderr
        assert _plists(env) == []
        assert "launchctl unload" in env["log"].read_text()

    def test_same_agent_reinstall_is_idempotent_one_plist(self, env):
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        assert _run("install-listener.sh", self.ARGS, env).returncode == 0
        assert len(_plists(env)) == 1  # reinstall replaces, never accumulates

    def test_wake_cmd_injection_rejected(self, env):
        for evil in ["do'evil", "a<b", "line1\nline2"]:
            r = _run("install-listener.sh",
                     [*self.ARGS, "--wake-cmd", evil], env)
            assert r.returncode != 0, f"accepted: {evil!r}"
        assert _plists(env) == []
