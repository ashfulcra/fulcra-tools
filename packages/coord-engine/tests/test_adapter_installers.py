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
        assert "briefing teamx --agent agent" in prompt
        assert "degraded section is not clear" in prompt
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
