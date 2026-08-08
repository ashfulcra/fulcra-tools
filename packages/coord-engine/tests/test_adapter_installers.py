"""Host-simulation tests for the three fulcra-agent-automation adapter installers
(codex / claude-code / openclaw) — the coord2 -> coord MIGRATED rename.

Each test either:
  * proves a FRESH install writes the NEW names only (no coord2 bytes), or
  * simulates a coord2-ERA installed host (old dirs / markers / automation /
    hooks entries / fence pairs) and asserts a re-run CONVERGES it to the new
    names with zero orphans, or
  * proves the PRE-COORD2 legacy artifacts (``fulcra-coord-hooks`` /
    ``<!-- fulcra-coord:begin -->`` / ``fulcra-coord-task-listener-``) are
    NEVER touched (non-collision, both directions), or
  * proves uninstall removes BOTH generations.

The codex + openclaw installers are stdlib Python loaded by path; the
claude-code installer is bash, run for real in a subprocess with a throwaway
HOME (mirroring test_installers.py).
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "fulcra-agent-automation" / "scripts"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cx = _load("install_codex_watch", "codex/install_codex_watch.py")
oc = _load("install_openclaw", "openclaw/install_openclaw.py")

# The exact PRE-COORD2 legacy strings the new names must never collide with.
LEGACY_HOOKS = "fulcra-coord-hooks"
LEGACY_FENCE_BEGIN = "<!-- fulcra-coord:begin -->"
LEGACY_LISTENER = "fulcra-coord-task-listener-"


# --------------------------------------------------------------------------- #
# Non-collision proof (both directions), mirroring the coexistence invariant.  #
# --------------------------------------------------------------------------- #

class TestNonCollision:
    def test_new_names_are_not_substrings_of_pre_coord2_legacy(self):
        # new  ⊄  legacy
        assert cx.MANAGED_DIRNAME not in LEGACY_HOOKS          # fulcra-agent-hooks
        assert cx.AUTOMATION_ID_PREFIX not in LEGACY_LISTENER  # coord-watch-
        assert oc._BEGIN not in LEGACY_FENCE_BEGIN
        assert "fulcra-agent:begin" not in LEGACY_FENCE_BEGIN

    def test_pre_coord2_legacy_names_are_not_substrings_of_new(self):
        # legacy  ⊄  new
        assert LEGACY_HOOKS not in cx.MANAGED_DIRNAME
        assert LEGACY_LISTENER not in cx.AUTOMATION_ID_PREFIX
        assert LEGACY_FENCE_BEGIN not in oc._BEGIN
        assert "fulcra-coord:begin" not in oc._BEGIN

    def test_new_vs_coord2_era_also_non_colliding(self):
        # the two generations THIS installer knows are mutually non-substring
        for new, old in ((cx.MANAGED_DIRNAME, cx.LEGACY_MANAGED_DIRNAME),
                         (cx.AUTOMATION_ID_PREFIX, cx.LEGACY_AUTOMATION_ID_PREFIX),
                         (oc._BEGIN, oc._LEGACY_BEGIN), (oc._END, oc._LEGACY_END)):
            assert new not in old and old not in new


# --------------------------------------------------------------------------- #
# Codex adapter.                                                               #
# --------------------------------------------------------------------------- #

def _codex_hooks_cmds(codex_dir: Path) -> list[str]:
    cfg = json.loads((codex_dir / "hooks.json").read_text())
    return [h.get("command", "")
            for entries in cfg.get("hooks", {}).values()
            if isinstance(entries, list)
            for e in entries if isinstance(e, dict)
            for h in e.get("hooks", []) if isinstance(h, dict)]


class TestCodex:
    def test_fresh_install_writes_new_names_only(self, tmp_path):
        d = tmp_path / "codex"
        plan = cx.install("teamx", "codex:h:r", codex_dir=d, thread_id="thr-1")
        assert (d / cx.MANAGED_DIRNAME).is_dir()
        assert not (d / cx.LEGACY_MANAGED_DIRNAME).exists()
        aid = plan["automation"]["id"]
        assert aid.startswith("coord-watch-") and "coord2" not in aid
        toml = (d / "automations" / aid / "automation.toml").read_text()
        assert 'name = "coord watch (codex:h:r)"' in toml
        cmds = _codex_hooks_cmds(d)
        # NB: pytest's tmp_path embeds the test-fn name, which itself can contain
        # "coord2" — so assert on the legacy DIRNAME/MARKER constants, not the
        # bare substring, to avoid false positives from the fixture path.
        assert cx.MANAGED_MARKER in "".join(cmds)
        assert cx.MANAGED_DIRNAME in "".join(cmds)
        assert not any(cx.LEGACY_MANAGED_DIRNAME in c or cx.LEGACY_MANAGED_MARKER in c
                       for c in cmds)
        assert "coord2" not in toml            # toml embeds no fixture path
        assert 'rrule = "FREQ=MINUTELY;INTERVAL=30"' in toml

    def test_watch_interval_is_configurable_and_validated(self, tmp_path):
        d = tmp_path / "codex"
        cx.install("teamx", "agent", codex_dir=d, thread_id="thr-1",
                   interval_minutes=90)
        toml = (d / "automations" / "coord-watch-agent" / "automation.toml").read_text()
        assert 'rrule = "FREQ=MINUTELY;INTERVAL=90"' in toml
        with pytest.raises(ValueError, match="interval_minutes"):
            cx.install("teamx", "agent", codex_dir=d, thread_id="thr-1",
                       interval_minutes=0)

    def test_watch_prompt_is_compact_but_keeps_safety_contract(self):
        prompt = cx.COORD_WATCH_PROMPT.format(team="teamx", agent="agent")
        assert len(prompt) < 900
        assert "inbox teamx --agent agent --json" in prompt
        assert "briefing teamx --agent agent" in prompt
        assert prompt.index("inbox teamx") < prompt.index("briefing teamx")
        assert "Do not rely on briefing's inbox section" in prompt
        assert "documented direct-listing fallback" in prompt
        assert "write and verify the exact required verdict before acking" in prompt

    def test_session_start_consumes_queued_wakes_before_briefing(self):
        assert "coord-engine wake consume" in cx.SESSION_START_SH
        assert 'WAKE_CONTEXT="$(' in cx.SESSION_START_SH
        assert "wake nudge" in cx.SESSION_START_SH

    def test_migrates_coord2_era_host_in_place(self, tmp_path):
        d = tmp_path / "codex"
        slug = "codex-h-r"
        # coord2-era managed hooks dir with a materialized script
        old_dir = d / cx.LEGACY_MANAGED_DIRNAME
        old_dir.mkdir(parents=True)
        (old_dir / "session-start.sh").write_text("#!/bin/bash\n# coord2\n")
        # coord2-era hooks.json entries carrying the OLD dir + marker, plus a
        # FOREIGN entry that must survive
        old_cmd = (f"{old_dir}/session-start.sh  # {cx.LEGACY_MANAGED_MARKER}")
        (d / "hooks.json").write_text(json.dumps({"hooks": {
            "SessionStart": [
                {"matcher": "startup|resume|clear|compact",
                 "hooks": [{"type": "command", "command": old_cmd}]},
                {"hooks": [{"type": "command", "command": "/usr/bin/foreign"}]},
            ],
            "PreCompact": [{"hooks": [{"type": "command",
                            "command": f"{old_dir}/pre-compact.sh"}]}],
        }}))
        # coord2-era automation dir with an armed thread id + created_at
        old_auto = d / "automations" / (cx.LEGACY_AUTOMATION_ID_PREFIX + slug)
        old_auto.mkdir(parents=True)
        (old_auto / "automation.toml").write_text(
            'version = 1\n'
            f'id = "{cx.LEGACY_AUTOMATION_ID_PREFIX + slug}"\n'
            'kind = "heartbeat"\n'
            'name = "coord2 watch (codex:h:r)"\n'
            'status = "ACTIVE"\n'
            'target_thread_id = "thr-armed"\n'
            'created_at = 111\n'
            'updated_at = 222\n')

        # re-run installer with NO --thread-id: must adopt the armed thread
        cx.install("teamx", "codex:h:r", codex_dir=d, thread_id=None)

        # new dir present, old dir GONE
        assert (d / cx.MANAGED_DIRNAME).is_dir()
        assert not old_dir.exists()
        # automation dir renamed, toml rewritten, thread + created preserved
        new_auto = d / "automations" / (cx.AUTOMATION_ID_PREFIX + slug)
        assert new_auto.is_dir() and not old_auto.exists()
        new_toml = (new_auto / "automation.toml").read_text()
        assert 'target_thread_id = "thr-armed"' in new_toml
        assert "created_at = 111" in new_toml
        assert 'name = "coord watch (codex:h:r)"' in new_toml and "coord2" not in new_toml
        # hooks.json: our entry updated (new dir, new marker), not duplicated;
        # foreign entry preserved; no coord2 bytes remain
        cmds = _codex_hooks_cmds(d)
        assert any(cx.MANAGED_DIRNAME in c and cx.MANAGED_MARKER in c for c in cmds)
        # legacy dir/marker fully stripped (bare "coord2" would false-match the
        # fixture path, which embeds this test's name)
        assert not any(cx.LEGACY_MANAGED_DIRNAME in c or cx.LEGACY_MANAGED_MARKER in c
                       for c in cmds)
        assert "/usr/bin/foreign" in cmds
        # exactly one SessionStart entry is ours (no orphan/dupe)
        ours = [c for c in cmds if cx.MANAGED_DIRNAME in c]
        assert len(ours) == 2  # session-start + pre-compact

    def test_uninstall_removes_both_generations(self, tmp_path):
        d = tmp_path / "codex"
        slug = "codex-h-r"
        # install new, then plant a coord2-era dir + automation alongside
        cx.install("teamx", "codex:h:r", codex_dir=d, thread_id="thr-1")
        (d / cx.LEGACY_MANAGED_DIRNAME).mkdir(parents=True)
        (d / cx.LEGACY_MANAGED_DIRNAME / "session-start.sh").write_text("x")
        old_auto = d / "automations" / (cx.LEGACY_AUTOMATION_ID_PREFIX + slug)
        old_auto.mkdir(parents=True)
        (old_auto / "automation.toml").write_text("version = 1\n")

        cx.install("teamx", "codex:h:r", codex_dir=d, uninstall=True)
        assert not (d / cx.MANAGED_DIRNAME).exists()
        assert not (d / cx.LEGACY_MANAGED_DIRNAME).exists()
        assert not (d / "automations" / (cx.AUTOMATION_ID_PREFIX + slug)).exists()
        assert not old_auto.exists()

    def test_pre_coord2_legacy_untouched(self, tmp_path):
        d = tmp_path / "codex"
        # a pre-coord2 legacy install: fulcra-coord-hooks dir + hooks entry +
        # fulcra-coord-task-listener automation. NONE of it is ours.
        legacy_dir = d / LEGACY_HOOKS
        legacy_dir.mkdir(parents=True)
        legacy_cmd = f"{legacy_dir}/session-start.sh"
        (d / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": legacy_cmd}]}]}}))
        legacy_auto = d / "automations" / (LEGACY_LISTENER + "abc")
        legacy_auto.mkdir(parents=True)
        (legacy_auto / "automation.toml").write_text("version = 1\n")

        cx.install("teamx", "codex:h:r", codex_dir=d, thread_id="thr-1")
        assert legacy_dir.is_dir()
        assert legacy_cmd in _codex_hooks_cmds(d)      # never stripped
        assert legacy_auto.is_dir()                     # never unlinked

        cx.install("teamx", "codex:h:r", codex_dir=d, uninstall=True)
        assert legacy_dir.is_dir()                      # uninstall spares it too
        assert legacy_cmd in _codex_hooks_cmds(d)
        assert legacy_auto.is_dir()

    def test_hostile_id_renders_bash_clean(self, tmp_path):
        d = tmp_path / "codex"
        for evil in ['bad"agent', "a$b", "x`y`", "a b/c", "id;rm -rf"]:
            cx.install("t;m", evil, codex_dir=d, thread_id="thr-1")
            for script in ("session-start.sh", "pre-compact.sh"):
                p = d / cx.MANAGED_DIRNAME / script
                r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
                assert r.returncode == 0, f"{evil!r}: {r.stderr}"


# --------------------------------------------------------------------------- #
# Claude Code adapter (bash, run for real).                                    #
# --------------------------------------------------------------------------- #

CLAUDE = SCRIPTS / "claude-code" / "install-claude-code.sh"
NEW_HOOKS = "fulcra-agent-hooks"
OLD_HOOKS = "fulcra-coord2-hooks"


def _run_claude(home: Path, args):
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C"}
    return subprocess.run(["bash", str(CLAUDE), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def _settings_cmds(home: Path) -> list[str]:
    d = json.loads((home / ".claude" / "settings.json").read_text())
    return [h.get("command", "")
            for rules in d.get("hooks", {}).values()
            for r in rules for h in r.get("hooks", [])]


class TestClaudeCode:
    def test_fresh_install_writes_new_dir_and_entries(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        r = _run_claude(home, ["teamx", "claude-code:h:r"])
        assert r.returncode == 0, r.stderr
        assert (home / ".claude" / NEW_HOOKS).is_dir()
        assert not (home / ".claude" / OLD_HOOKS).exists()
        cmds = _settings_cmds(home)
        assert cmds and all(NEW_HOOKS in c for c in cmds)
        # NB: check the OLD dirname, not bare "coord2" (the fixture path can
        # contain the test-fn name, which contains "coord2").
        assert not any(OLD_HOOKS in c for c in cmds)
        session_start = (
            home / ".claude" / NEW_HOOKS / "session-start.sh").read_text()
        assert "coord-engine wake consume" in session_start
        assert "WAKE_CONTEXT=" in session_start

    def test_migrates_coord2_era_host(self, tmp_path):
        home = tmp_path / "home"
        cdir = home / ".claude"
        old_dir = cdir / OLD_HOOKS
        old_dir.mkdir(parents=True)
        (old_dir / "session-start.sh").write_text("#!/bin/bash\n")
        old_cmd = f"{old_dir}/session-start.sh"
        cdir_settings = cdir / "settings.json"
        cdir_settings.write_text(json.dumps({"hooks": {
            "SessionStart": [
                {"matcher": "startup|resume|clear|compact",
                 "hooks": [{"type": "command", "command": old_cmd}]},
                {"hooks": [{"type": "command", "command": "/usr/bin/foreign"}]},
            ]}}))
        r = _run_claude(home, ["teamx", "claude-code:h:r"])
        assert r.returncode == 0, r.stderr
        assert (cdir / NEW_HOOKS).is_dir()
        assert not old_dir.exists()                     # old dir removed
        cmds = _settings_cmds(home)
        assert not any(OLD_HOOKS in c for c in cmds)    # old entry stripped
        assert any(NEW_HOOKS in c for c in cmds)        # new entry present
        assert "/usr/bin/foreign" in cmds               # foreign preserved
        # no duplicate SessionStart entries for us
        ss = json.loads(cdir_settings.read_text())["hooks"]["SessionStart"]
        ours = [r for r in ss if any(NEW_HOOKS in h["command"] for h in r["hooks"])]
        assert len(ours) == 1

    def test_uninstall_removes_both_generations(self, tmp_path):
        home = tmp_path / "home"
        cdir = home / ".claude"
        cdir.mkdir(parents=True)
        _run_claude(home, ["teamx", "claude-code:h:r"])
        # plant a coord2-era dir + settings entry
        old_dir = cdir / OLD_HOOKS
        old_dir.mkdir(parents=True)
        d = json.loads((cdir / "settings.json").read_text())
        d["hooks"]["SessionStart"].append(
            {"hooks": [{"type": "command", "command": f"{old_dir}/session-start.sh"}]})
        (cdir / "settings.json").write_text(json.dumps(d))
        r = _run_claude(home, ["--uninstall", "teamx", "claude-code:h:r"])
        assert r.returncode == 0, r.stderr
        assert not (cdir / NEW_HOOKS).exists()
        assert not old_dir.exists()
        assert not any(OLD_HOOKS in c or NEW_HOOKS in c for c in _settings_cmds(home))

    def test_pre_coord2_legacy_untouched(self, tmp_path):
        home = tmp_path / "home"
        cdir = home / ".claude"
        legacy_dir = cdir / LEGACY_HOOKS
        legacy_dir.mkdir(parents=True)
        legacy_cmd = f"{legacy_dir}/session-start.sh"
        (cdir / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": legacy_cmd}]}]}}))
        assert _run_claude(home, ["teamx", "claude-code:h:r"]).returncode == 0
        assert legacy_dir.is_dir() and legacy_cmd in _settings_cmds(home)
        assert _run_claude(home, ["--uninstall", "teamx", "claude-code:h:r"]).returncode == 0
        assert legacy_dir.is_dir() and legacy_cmd in _settings_cmds(home)


# --------------------------------------------------------------------------- #
# OpenClaw adapter.                                                            #
# --------------------------------------------------------------------------- #

class TestOpenClaw:
    def test_fresh_install_writes_new_fence_only(self, tmp_path):
        oc.install("teamx", "agent", workspace=tmp_path)
        hb = (tmp_path / "HEARTBEAT.md").read_text()
        assert oc._BEGIN in hb and oc._END in hb
        assert "coord2" not in hb and "fulcra-agent:begin" in hb
        assert "on coord team teamx" in hb
        managed = hb.split(oc._BEGIN, 1)[1].split(oc._END, 1)[0]
        assert len(managed) < 800
        assert "degraded section is not clear" in managed
        assert "write and verify the exact required verdict before acking" in " ".join(managed.split())

    def test_migrates_coord2_era_fence_preserving_user_content(self, tmp_path):
        # a workspace with an OLD coord2 fence around a body, plus USER prose
        # both before and after the managed block
        old_block = (f"{oc._LEGACY_BEGIN}\n"
                     "On each heartbeat, as agent on coord2 team teamx:\n"
                     "1. do the old thing\n"
                     f"{oc._LEGACY_END}\n")
        hb = tmp_path / "HEARTBEAT.md"
        hb.write_text("# My own notes\nkeep me above\n\n" + old_block +
                      "\nkeep me below too\n")
        oc.install("teamx", "agent", workspace=tmp_path)
        out = hb.read_text()
        # old fence gone, new fence present, user content on both sides preserved
        assert oc._LEGACY_BEGIN not in out and "coord2" not in out
        assert oc._BEGIN in out and oc._END in out
        assert "keep me above" in out and "keep me below too" in out
        assert "on coord team teamx" in out

    def test_uninstall_removes_both_generations(self, tmp_path):
        # HEARTBEAT holds a NEW fence; BOOT holds a coord2-era fence. Uninstall
        # strips whichever it finds and deletes husks.
        oc.install("teamx", "agent", workspace=tmp_path)     # writes new fences
        boot = tmp_path / "BOOT.md"
        boot.write_text(f"{oc._LEGACY_BEGIN}\nold boot body\n{oc._LEGACY_END}\n")
        oc.install("teamx", "agent", workspace=tmp_path, uninstall=True)
        assert not (tmp_path / "HEARTBEAT.md").exists()      # husk deleted
        assert not boot.exists()                              # coord2 husk deleted

    def test_pre_coord2_legacy_fence_untouched(self, tmp_path):
        hb = tmp_path / "HEARTBEAT.md"
        legacy = (f"{LEGACY_FENCE_BEGIN}\nlegacy fulcra-coord body\n"
                  "<!-- fulcra-coord:end -->\n")
        hb.write_text(legacy)
        oc.install("teamx", "agent", workspace=tmp_path)
        out = hb.read_text()
        assert LEGACY_FENCE_BEGIN in out and "legacy fulcra-coord body" in out
        assert oc._BEGIN in out                               # our block appended after
        # uninstall strips only ours; legacy block survives
        oc.install("teamx", "agent", workspace=tmp_path, uninstall=True)
        out2 = hb.read_text()
        assert LEGACY_FENCE_BEGIN in out2 and oc._BEGIN not in out2

    def test_refuses_unbalanced_new_marker(self, tmp_path):
        hb = tmp_path / "HEARTBEAT.md"
        hb.write_text(f"{oc._BEGIN}\norphan begin, no end\n")
        with pytest.raises(oc.MarkerIntegrityError):
            oc.install("teamx", "agent", workspace=tmp_path)

    def test_refuses_unbalanced_coord2_era_marker(self, tmp_path):
        hb = tmp_path / "HEARTBEAT.md"
        hb.write_text(f"{oc._LEGACY_END}\nend with no begin\n")
        with pytest.raises(oc.MarkerIntegrityError):
            oc.install("teamx", "agent", workspace=tmp_path)

    def test_refuses_mismatched_generation_fence(self, tmp_path):
        hb = tmp_path / "HEARTBEAT.md"
        hb.write_text(f"{oc._BEGIN}\nbody\n{oc._LEGACY_END}\n")
        with pytest.raises(oc.MarkerIntegrityError):
            oc.install("teamx", "agent", workspace=tmp_path)

    def test_code_fence_awareness_ignores_documented_coord2_marker(self, tmp_path):
        # a coord2 marker shown inside a fenced code sample is NOT a real block
        hb = tmp_path / "HEARTBEAT.md"
        hb.write_text("# docs\n```\n" + oc._LEGACY_BEGIN + "\nsample\n"
                      + oc._LEGACY_END + "\n```\nreal prose\n")
        # must not raise (the fenced markers are inert) and must append our block
        oc.install("teamx", "agent", workspace=tmp_path)
        out = hb.read_text()
        assert oc._BEGIN in out and "real prose" in out
        # the documented sample is preserved verbatim inside the code fence
        assert "```" in out and out.count(oc._LEGACY_BEGIN) == 1


class TestCodexWatchFailsClosed:
    """P1 (codex-reviewer, 2026-07-10): the watcher reported WATCH_OK while reviews
    were waiting — it read a fold that had degraded and could not tell.

    The hook stacks THREE truncations on the briefing read: `2>/dev/null`,
    `| head -60`, and a final `ctx[:4000]`. Every one discards the TAIL, and the
    tail is where a fold's degraded markers and verdict live. stderr is the one
    stream truncation does not reach — and it was being thrown away."""

    def test_session_start_keeps_briefing_stderr_instead_of_discarding_it(self):
        sh = cx.SESSION_START_SH
        assert 'coord-engine briefing "$TEAM" --agent "$AGENT" 2>/dev/null' not in sh, (
            "briefing stderr is the trust signal that survives stdout truncation; "
            "sending it to /dev/null is the defect this closes")
        assert "BRIEF_ERR" in sh

    def test_degraded_block_is_placed_before_the_truncated_payload(self):
        """Order is the whole point: a signal appended after an unbounded payload is
        a signal inside the part that gets cut."""
        sh = cx.SESSION_START_SH
        assert sh.index("coord degraded:") < sh.index("coord briefing (stdout")

    def test_truncated_stdout_is_labelled_as_truncated(self):
        # Absence of a marker in a truncated stream is not evidence of its absence.
        assert "absence of a marker here is NOT" in cx.SESSION_START_SH

    def test_prompt_forbids_watch_ok_on_a_degraded_read(self):
        prompt = cx.COORD_WATCH_PROMPT.format(team="teamx", agent="agent")
        assert "WATCH_OK claims you looked and saw everything" in prompt
        assert "coord degraded:" in prompt      # the exact block the hook emits
        for phrase in ("degraded", "timed-out", "not that you saw nothing"):
            assert phrase in prompt, phrase
        # The compactness budget above is real — this prompt is re-sent every tick.
        assert len(prompt) < 900

    def test_the_hook_still_renders_and_stays_bounded(self, tmp_path):
        """The fix must not break rendering or reintroduce an unbounded dump."""
        cx.install("teamx", "agent", codex_dir=tmp_path, thread_id="thr-1")
        hook = (tmp_path / cx.MANAGED_DIRNAME / "session-start.sh").read_text()
        assert "__TEAM__" not in hook and "__AGENT__" not in hook
        assert "head -60" in hook and "ctx[:4000]" in hook
        assert "head -6 " in hook          # the stderr capture is bounded too


def _render_and_run(tmp_path, stub_body, agent="cm"):
    """Render the hook and EXECUTE it against a stub coord-engine.

    Grepping the template proved nothing: an earlier version of this fix passed
    every string assertion while emitting no degraded block at runtime. Only
    running it caught that."""
    import json as _j
    import os
    import stat as _stat
    import subprocess
    cx.install("fulcra", agent, codex_dir=tmp_path, thread_id="t1")
    hook = tmp_path / cx.MANAGED_DIRNAME / "session-start.sh"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "coord-engine"
    stub.write_text(stub_body)
    stub.chmod(stub.stat().st_mode | _stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    r = subprocess.run(["bash", str(hook)], input="{}", capture_output=True,
                       text=True, env=env)
    return _j.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


_STUB_WARNS_AT_THE_TAIL = """#!/bin/bash
case "$1" in
  briefing)
    for i in $(seq 1 200); do echo "row $i"; done
    echo "briefing: presence section unavailable (TransportError)" >&2 ;;
  *) echo "" ;;
esac
exit 0
"""


def test_a_fold_that_warns_AFTER_a_long_payload_still_reaches_the_model(tmp_path):
    """The regression the P1 asked for, and the one that nearly shipped broken.

    Piping to `head -60` closes the pipe and SIGPIPEs the producer, so a fold whose
    degraded marker comes after a long payload is killed before writing it to
    stdout OR stderr — the warning disappears and the tick reads clean. The hook
    therefore runs the command to completion into a file and truncates afterwards."""
    ctx = _render_and_run(tmp_path, _STUB_WARNS_AT_THE_TAIL)
    assert "coord degraded:" in ctx, (
        "a tail-emitted warning was lost — the truncation killed the producer")
    assert "presence section unavailable" in ctx
    assert ctx.index("coord degraded:") < ctx.index("coord briefing (stdout")


# --- 567 + 569 TOGETHER ------------------------------------------------------
# codex-reviewer, PR 569 r1: PR 567 makes the folds emit a verdict envelope on
# stderr on EVERY run, healthy ones included. This hook treated any stderr as a
# degraded signal — so with both changes present, every healthy watch would be
# labelled degraded and the new prompt would forbid WATCH_OK forever. I shipped
# both PRs claiming they "compose" without ever running them together.

_HEALTHY_ENVELOPE_STUB = """#!/bin/bash
case "$1" in
  briefing)
    echo "  board: active=1"
    echo "briefing: 3 item(s), inbox=0, reviews=0, degraded=0, rc=0" >&2 ;;
  *) echo "" ;;
esac
exit 0
"""

_DEGRADED_ENVELOPE_STUB = """#!/bin/bash
case "$1" in
  briefing)
    echo "  board: active=1"
    echo "briefing: 3 item(s), inbox=0, reviews=0, degraded=2, rc=0" >&2 ;;
  *) echo "" ;;
esac
exit 0
"""


def test_a_healthy_envelope_is_not_treated_as_degradation(tmp_path):
    """The interaction defect: healthy stderr must not forbid WATCH_OK."""
    ctx = _render_and_run(tmp_path, _HEALTHY_ENVELOPE_STUB)
    assert "coord degraded:" not in ctx, (
        "a healthy `degraded=0, rc=0` envelope was misread as degradation — every "
        "healthy tick would be blocked from WATCH_OK")
    # and it is kept as POSITIVE evidence that the fold completed
    assert "coord verdict (fold completed clean)" in ctx
    assert "degraded=0, rc=0" in ctx


def test_a_nonzero_envelope_still_fails_closed(tmp_path):
    ctx = _render_and_run(tmp_path, _DEGRADED_ENVELOPE_STUB)
    assert "coord degraded:" in ctx
    assert "NONZERO verdict envelope" in ctx
    assert "coord verdict (fold completed clean)" not in ctx


def test_unclassified_stderr_still_fails_closed(tmp_path):
    """Anything that is not an envelope at all stays fail-closed — the original
    `briefing: <section> unavailable` warnings must keep working."""
    ctx = _render_and_run(tmp_path, _STUB_WARNS_AT_THE_TAIL)
    assert "coord degraded:" in ctx
    assert "unclassified output to stderr" in ctx


def test_mktemp_failure_captures_nothing_rather_than_a_guessable_path(tmp_path):
    """codex-reviewer, PR 569 r1: the old fallback wrote to /tmp/coord-brief-*.$$,
    and `>` follows a symlink — a local process could aim that name at any file
    the agent can write. There is no safe predictable name, so fail closed."""
    sh = cx.SESSION_START_SH
    # Assert the CONSTRUCT is gone, not the string: the comment above the fix names
    # the guessable path as the thing being avoided, and a blunt substring check
    # would forbid explaining the defect.
    assert "|| echo /tmp/coord-brief" not in sh, "predictable-path fallback is back"
    for var in ("BRIEF_ERR_FILE", "BRIEF_OUT_FILE"):
        assert f'{var}="$(mktemp 2>/dev/null)" || {var}=""' in sh
    assert "mktemp unavailable, briefing not captured" in sh
    assert "trap 'rm -f" in sh          # interruption cannot strand temp files
