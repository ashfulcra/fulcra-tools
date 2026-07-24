"""The FIRST real host-local wake adapter: `macos-notify` + the invoker seam.

W5.5 gave the host executor one invoker seam that returned `unconfigured` for
every adapter, and no adapter script existed — nothing could ever be delivered.
This pins the seam's contract and the script's behaviour:

- PROVISIONING is explicit (`COORD_WAKE_ADAPTER_DIR`). An un-provisioned host
  reports `unconfigured`, so the wake stays VISIBLY QUEUED — never a silent
  drop, never a burned retry. That is also why this change is INERT: with the
  env unset (the default everywhere, including this suite) nothing fires.
- exit 0 ⇒ `delivered`; non-zero / un-spawnable / TIMEOUT ⇒ `failed`.
- BOUNDED: a hung script is killed and reported `failed` — it can never wedge
  the executor.
- NUDGE-ONLY (plan §2): the ONLY things that reach the script are the agent id,
  the idempotency key, and a STATIC reason string. No per-event command, no
  shell, no URL, no payload — structurally, because the argv is built from
  exactly those three fields and a module constant.
- every other host-local adapter still reports `unconfigured` (its script is
  W6's).

No test posts a real notification: the seam tests run stub scripts, and the
script tests run against a recording `osascript` shim or with `osascript`
removed from PATH.
"""

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coord_engine import cli, router, wake_adapters
from coord_engine_test_helpers import FakeTransport

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills/fulcra-agent-automation/scripts/wake/macos-notify.sh"

TEAM = "t"
RP = f"team/{TEAM}/_coord/router/"
HOST = "mac-mini-1"
AGENT = "worker-a"
KEY = "item-1:worker-a"

PINNED_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _inv(adapter="macos-notify", agent=AGENT, key=KEY, **extra):
    inv = {"adapter": adapter, "agent": agent, "idempotency_key": key,
           "message": f"wake({agent}): a directed item is on your bus [{key}]."}
    inv.update(extra)
    return inv


def _provision(tmp_path, monkeypatch, body, name="macos-notify.sh",
               mode=0o755) -> Path:
    """Write a STUB adapter script into a throwaway dir and point the host's
    provisioning env at it. Nothing real is ever invoked."""
    d = tmp_path / "wake-adapters"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body)
    p.chmod(mode)
    monkeypatch.setenv(wake_adapters.WAKE_ADAPTER_DIR_ENV, str(d))
    return p


# --- seam contract -----------------------------------------------------------

def test_unprovisioned_host_is_unconfigured(monkeypatch):
    """No provisioning env → `unconfigured`, so the wake stays visibly queued."""
    monkeypatch.delenv(wake_adapters.WAKE_ADAPTER_DIR_ENV, raising=False)
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "unconfigured"
    assert "macos-notify" in detail


def test_absent_script_is_unconfigured(tmp_path, monkeypatch):
    """Provisioned dir but no script for THIS adapter → still `unconfigured`."""
    d = tmp_path / "empty"
    d.mkdir()
    monkeypatch.setenv(wake_adapters.WAKE_ADAPTER_DIR_ENV, str(d))
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "unconfigured"
    assert "macos-notify.sh" in detail


def test_non_executable_script_is_unconfigured(tmp_path, monkeypatch):
    """Half-provisioned (present, not executable) is a provisioning fault, not a
    delivery attempt: unconfigured keeps the wake queued and burns no retry."""
    _provision(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n", mode=0o644)
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "unconfigured"
    assert "not executable" in detail


def test_exit_zero_is_delivered(tmp_path, monkeypatch):
    _provision(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "delivered"
    assert AGENT in detail


def test_non_zero_exit_is_failed_with_the_scripts_stderr(tmp_path, monkeypatch):
    _provision(tmp_path, monkeypatch,
               "#!/bin/sh\necho 'osascript not found' >&2\nexit 127\n")
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "failed"
    assert "127" in detail and "osascript not found" in detail


def test_unspawnable_script_is_failed(tmp_path, monkeypatch):
    """Executable bit set but not a runnable image → `failed`, never a crash."""
    p = _provision(tmp_path, monkeypatch, "\x7fELF not really a binary\n")
    assert os.access(p, os.X_OK)
    status, detail = cli._default_host_adapter_invoke(_inv())
    assert status == "failed"
    assert detail


def test_hung_script_is_killed_and_reported_failed(tmp_path, monkeypatch):
    """BOUNDED: a hung adapter must never wedge the executor."""
    _provision(tmp_path, monkeypatch, "#!/bin/sh\nsleep 60\n")
    monkeypatch.setenv(wake_adapters.WAKE_ADAPTER_TIMEOUT_ENV, "0.5")
    t0 = time.monotonic()
    status, detail = cli._default_host_adapter_invoke(_inv())
    elapsed = time.monotonic() - t0
    assert status == "failed"
    assert "timed out" in detail
    assert elapsed < 20, f"invoker blocked {elapsed:.1f}s on a hung adapter"


def test_other_host_local_adapters_stay_unconfigured(tmp_path, monkeypatch):
    """Only macos-notify is wired here; W6 owns the rest. Even with a script
    sitting in the provisioned dir, they report `unconfigured`."""
    for adapter in sorted(router.ADAPTERS_HOST_LOCAL - {"macos-notify"}):
        _provision(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n",
                   name=f"{adapter}.sh")
        status, _ = cli._default_host_adapter_invoke(_inv(adapter=adapter))
        assert status == "unconfigured", adapter


# --- nudge-only content rule (plan §2) --------------------------------------

RECORDER = """#!/bin/sh
{{ printf 'ARGV:%s\\n' "$@"; printf 'ENV:%s\\n' "$(env)"; }} > '{record}'
exit 0
"""


def test_only_agent_key_and_a_static_reason_reach_the_script(
        tmp_path, monkeypatch):
    """The exact bytes the adapter receives. An invocation polluted with an
    actionable payload delivers NONE of it: argv is built from the agent, the
    key and a module constant, so the property is structural."""
    record = tmp_path / "argv.txt"
    p = _provision(tmp_path, monkeypatch, RECORDER.format(record=record))
    poison = {
        "command": "POISON-CMD-rm-rf-slash",
        "cmd": "POISON-CMD2",
        "exec": "POISON-EXEC",
        "run": "POISON-RUN",
        "payload": {"POISON-PAYLOAD": True},
        "url": "https://poison.example/POISON-URL",
        "session_patch": {"POISON-PATCH": 1},
        "message": "wake ... POISON-MESSAGE-BODY",
    }
    status, _ = cli._default_host_adapter_invoke(_inv(**poison))
    assert status == "delivered"

    text = record.read_text()
    argv = [line[len("ARGV:"):] for line in text.splitlines()
            if line.startswith("ARGV:")]
    # argv[0] is the resolved script itself; these are ALL of its arguments
    assert argv == ["--agent", AGENT, "--key", KEY,
                    "--reason", wake_adapters.NUDGE_REASON]
    assert wake_adapters.adapter_script("macos-notify") == p
    # nothing actionable reached the process — argv OR environment. (The tokens
    # are unique to this invocation, so a hit means `inv` content crossed the
    # boundary; the inherited parent environment is untouched by design.)
    for token in ("POISON", "rm-rf", "poison.example"):
        assert token not in text, f"{token!r} reached the adapter process"


def test_static_reason_is_a_constant_not_derived_from_the_entry():
    """The reason is fixed text: it cannot carry per-event content."""
    reason = wake_adapters.NUDGE_REASON
    assert isinstance(reason, str) and reason
    for token in ("{", "}", "%s", "http", "$(", "`"):
        assert token not in reason


def test_hostile_agent_or_key_is_refused_before_the_script_runs(
        tmp_path, monkeypatch):
    """Fields outside the accepted charset never reach the adapter at all."""
    record = tmp_path / "argv.txt"
    _provision(tmp_path, monkeypatch, RECORDER.format(record=record))
    for bad in ("worker-a; rm -rf /", "-oh-no", "worker\na", "$(whoami)", ""):
        status, detail = cli._default_host_adapter_invoke(_inv(agent=bad))
        assert status == "failed", bad
        assert "charset" in detail
    assert not record.exists(), "the adapter ran on a rejected invocation"


# --- executor integration ----------------------------------------------------

def _host_entry(adapter="macos-notify", source="item-1"):
    return {"agent": AGENT, "reason": "check your bus", "source_shard": source,
            "priority": "P1", "queued_at": "2026-07-23T12:00:00Z",
            "not_before": "2026-07-23T12:00:00Z",
            "adapter": adapter, "executor": HOST}


def _seed(t, entry):
    key = router.idempotency_key(entry["source_shard"], entry["agent"])
    path = RP + "queue/" + router.queue_filename(entry["agent"], key)
    t.put(path, json.dumps(entry))
    t.put(RP + "config.json", json.dumps(
        {AGENT: {"enabled": True}, "executors": [HOST]}))
    return path


def _args():
    import argparse
    return argparse.Namespace(team=TEAM, host=HOST, once=True, dry_run=False)


def test_provisioned_host_delivers_through_the_default_invoker(
        tmp_path, monkeypatch):
    """End-to-end through the REAL default invoker (a stub script stands in for
    the adapter): the executor writes a delivery record and drains the entry."""
    _provision(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    t = FakeTransport()
    qpath = _seed(t, _host_entry())
    counts = cli._router_execute_host(_args(), t)
    assert counts["delivered"] == 1
    assert qpath not in t.store


def test_unprovisioned_host_leaves_the_wake_visibly_queued(monkeypatch):
    """The fail-visible property this change must preserve: no provisioning ⇒
    the entry is still in the queue, no retry burned, no records written."""
    monkeypatch.delenv(wake_adapters.WAKE_ADAPTER_DIR_ENV, raising=False)
    t = FakeTransport()
    qpath = _seed(t, _host_entry())
    counts = cli._router_execute_host(_args(), t)
    assert counts["unconfigured"] == 1
    assert counts["delivered"] == 0 and counts["dead_lettered"] == 0
    assert qpath in t.store
    assert json.loads(t.store[qpath]).get("attempts") is None


def test_suite_never_inherits_host_provisioning():
    """Inertness guard: the provisioning env is cleared for every test, so a
    developer's real provisioned host cannot make the suite fire a wake."""
    assert wake_adapters.WAKE_ADAPTER_DIR_ENV not in os.environ


# --- the script itself -------------------------------------------------------

def _stub_osascript(tmp_path, body="exit 0") -> Path:
    """A PATH dir holding a recording `osascript` shim — no real notification."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    shim = d / "osascript"
    shim.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{tmp_path}/osa.txt'\n"
                    f"{body}\n")
    shim.chmod(0o755)
    return d


def _run(argv, env_path, timeout=15):
    env = dict(os.environ)
    env["PATH"] = env_path
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env=env, stdin=subprocess.DEVNULL)


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "adapter script is not executable"


def test_script_resolves_as_the_provisioned_adapter(monkeypatch):
    """A host provisioned at the skill's wake/ dir resolves THIS script.
    Resolution only — the script is not run."""
    monkeypatch.setenv(wake_adapters.WAKE_ADAPTER_DIR_ENV, str(SCRIPT.parent))
    assert wake_adapters.adapter_script("macos-notify") == SCRIPT


def test_script_fails_clearly_and_fast_without_osascript(tmp_path):
    """No osascript (non-macOS, or a stripped PATH): non-zero + a clear message,
    and it must NOT hang."""
    import shutil
    empty = tmp_path / "nobin"
    empty.mkdir()
    bash = shutil.which("bash") or "/bin/bash"
    t0 = time.monotonic()
    # bash by absolute path: PATH holds nothing, so osascript cannot resolve
    r = _run([bash, str(SCRIPT), "--agent", AGENT, "--key", KEY,
              "--reason", "check your bus"], str(empty))
    assert r.returncode != 0
    assert "osascript" in r.stderr
    assert time.monotonic() - t0 < 10


def test_script_passes_text_as_osascript_arguments_never_interpolated(tmp_path):
    """Injection-proof by construction: the AppleScript source is a fixed
    `on run argv` program; agent/key/reason arrive as ARGUMENTS."""
    binp = _stub_osascript(tmp_path)
    r = _run([str(SCRIPT), "--agent", AGENT, "--key", KEY,
              "--reason", "check your bus"], f"{binp}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    passed = (tmp_path / "osa.txt").read_text().splitlines()
    src = [passed[i + 1] for i, a in enumerate(passed) if a == "-e"]
    assert any("on run argv" in line for line in src)
    # the AppleScript source is fixed: no caller byte is interpolated into it
    assert all(AGENT not in line and KEY not in line for line in src)
    tail = passed[max(i for i, a in enumerate(passed) if a == "-e") + 2:]
    assert any(AGENT in a for a in tail)
    assert any(KEY in a for a in tail)


def test_script_propagates_an_osascript_failure(tmp_path):
    binp = _stub_osascript(tmp_path, body="exit 5")
    r = _run([str(SCRIPT), "--agent", AGENT, "--key", KEY,
              "--reason", "check your bus"], f"{binp}:{os.environ['PATH']}")
    assert r.returncode != 0
    assert "macos-notify" in r.stderr


@pytest.mark.parametrize("argv", [
    [],
    ["--agent", AGENT],
    ["--agent", "bad;agent", "--key", KEY, "--reason", "r"],
    ["--agent", AGENT, "--key", "bad key", "--reason", "r"],
    ["--agent", AGENT, "--key", KEY, "--reason", "r", "--command", "rm -rf /"],
])
def test_script_refuses_bad_or_unknown_arguments(tmp_path, argv):
    """Usage errors exit non-zero without invoking osascript — notably an
    unknown `--command` flag: this adapter has no command surface."""
    binp = _stub_osascript(tmp_path)
    r = _run([str(SCRIPT), *argv], f"{binp}:{os.environ['PATH']}")
    assert r.returncode != 0
    assert not (tmp_path / "osa.txt").exists()


def test_script_spawns_nothing_but_osascript():
    """Structural read of the script: no session-spawning binary, no network,
    no eval of anything passed in."""
    body = SCRIPT.read_text()
    for forbidden in ("curl", "wget", "eval ", "codex ", "claude ", "open -a",
                      "launchctl", "nohup", "python"):
        assert forbidden not in body, f"{forbidden!r} in the notify adapter"


def test_shellcheck_clean_if_available():
    import shutil
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, str(SCRIPT)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
