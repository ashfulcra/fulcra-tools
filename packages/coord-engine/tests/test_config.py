"""The shared env-var parser (``coord_engine.config``) and its policy — the ONE
place NaN/inf/flag-vs-env handling is defined, so the family of budgets/timeouts
can never drift apart again (the drift codex flagged across the copy-pasted
per-helper bodies). Also: each budget wrapper's default, and a docs-vs-code check
that every ``COORD_*`` name in the README env table is actually read by the code.
"""

import pathlib
import re

import pytest

from coord_engine import config, cli
from coord_engine import transport as tr
from coord_engine_test_helpers import FakeTransport


# --- env_float: the positive-finite policy, resolved override > env > alias > default ---

def test_env_float_reads_env(monkeypatch):
    monkeypatch.setenv("COORD_X", "12.5")
    assert config.env_float("COORD_X", 30.0) == 12.5


def test_env_float_default_when_absent(monkeypatch):
    monkeypatch.delenv("COORD_X", raising=False)
    assert config.env_float("COORD_X", 30.0) == 30.0


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-5", "nan", "inf", "-inf", "1e999"])
def test_env_float_bad_values_fall_back_to_default(monkeypatch, bad):
    """Unparseable, NaN, ±inf (``1e999`` parses to +inf), and <= minimum all fall
    back — a bad env value must never disable a bound."""
    monkeypatch.setenv("COORD_X", bad)
    assert config.env_float("COORD_X", 30.0) == 30.0


def test_env_float_minimum_is_a_strict_floor(monkeypatch):
    monkeypatch.setenv("COORD_X", "5")
    assert config.env_float("COORD_X", 30.0, minimum=5.0) == 30.0   # not > 5
    assert config.env_float("COORD_X", 30.0, minimum=4.0) == 5.0    # > 4


def test_env_float_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("COORD_X", "8")
    assert config.env_float("COORD_X", 30.0, override=3.0) == 3.0
    # a bad override still falls back (it does not silently leak env or default-skip)
    assert config.env_float("COORD_X", 30.0, override="nope") == 30.0


def test_env_float_override_none_reads_env(monkeypatch):
    monkeypatch.setenv("COORD_X", "8")
    assert config.env_float("COORD_X", 30.0, override=None) == 8.0


def test_env_float_alias_only_when_canonical_absent(monkeypatch):
    monkeypatch.delenv("COORD_X", raising=False)
    monkeypatch.setenv("LEGACY_X", "7")
    assert config.env_float("COORD_X", 30.0, aliases=("LEGACY_X",)) == 7.0
    # canonical wins when both are set
    monkeypatch.setenv("COORD_X", "9")
    assert config.env_float("COORD_X", 30.0, aliases=("LEGACY_X",)) == 9.0


# --- env_int ---

def test_env_int_policy(monkeypatch):
    monkeypatch.setenv("COORD_N", "5")
    assert config.env_int("COORD_N", 16) == 5
    for bad in ("bananas", "0", "-1", "5.0", ""):
        monkeypatch.setenv("COORD_N", bad)
        assert config.env_int("COORD_N", 16) == 16, bad


# --- each wrapper keeps only its domain default + env name ---

def test_budget_wrappers_default_when_env_absent(monkeypatch):
    for name in ("COORD_REVIEW_FOLD_BUDGET", "COORD_BRIEFING_BUDGET",
                 "COORD_LISTEN_CLASSIFY_BUDGET", "COORD_OVERLAY_BUDGET",
                 "COORD_THREADS_FOLD_BUDGET", "COORD_OVERLAY_CAP",
                 "COORD_TRANSPORT_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    assert cli._review_fold_budget() == cli.DEFAULT_REVIEW_FOLD_BUDGET
    assert cli._briefing_budget() == cli.DEFAULT_BRIEFING_BUDGET
    assert cli._listen_classify_budget() == cli.DEFAULT_LISTEN_CLASSIFY_BUDGET
    assert cli._overlay_budget() == cli.DEFAULT_OVERLAY_BUDGET
    assert cli._threads_fold_budget() == cli.DEFAULT_THREADS_FOLD_BUDGET
    assert cli._overlay_cap() == cli.DEFAULT_OVERLAY_CAP
    assert tr._transport_timeout() == tr.DEFAULT_TRANSPORT_TIMEOUT


@pytest.mark.parametrize("wrapper,env,default", [
    ("_review_fold_budget", "COORD_REVIEW_FOLD_BUDGET", "DEFAULT_REVIEW_FOLD_BUDGET"),
    ("_briefing_budget", "COORD_BRIEFING_BUDGET", "DEFAULT_BRIEFING_BUDGET"),
    ("_listen_classify_budget", "COORD_LISTEN_CLASSIFY_BUDGET", "DEFAULT_LISTEN_CLASSIFY_BUDGET"),
    ("_overlay_budget", "COORD_OVERLAY_BUDGET", "DEFAULT_OVERLAY_BUDGET"),
    ("_threads_fold_budget", "COORD_THREADS_FOLD_BUDGET", "DEFAULT_THREADS_FOLD_BUDGET"),
])
def test_budget_wrappers_honor_env_and_reject_bad(monkeypatch, wrapper, env, default):
    fn = getattr(cli, wrapper)
    default_val = getattr(cli, default)
    monkeypatch.setenv(env, "3.5")
    assert fn() == 3.5
    for bad in ("nan", "inf", "0", "-1", "junk"):
        monkeypatch.setenv(env, bad)
        assert fn() == default_val, (env, bad)


# --- threads windows: same positive-finite parser, flag > env (absorbs ENG-1-9) ---
#
# COORD_THREADS_SILENCE_DAYS + COORD_THREADS_INTENT_GRACE_HOURS used to parse with
# a bare float()+`v>0`, so both ACCEPTED inf — contradicting the README contract
# ("all numeric knobs positive-finite via one shared parser"). Routed through
# config.env_float(env, default, override=flag): flag>env precedence via override,
# minimum=0.0 floor, NaN/inf-safe. This closes ENG-1-9 (threads inf-unify).

_THREADS_ENVS = ["COORD_THREADS_SILENCE_DAYS", "COORD_THREADS_INTENT_GRACE_HOURS"]


@pytest.mark.parametrize("env", _THREADS_ENVS)
def test_threads_window_rejects_inf_nan_zero_via_env(monkeypatch, env):
    for bad in ("inf", "-inf", "nan", "1e999", "0", "-1", "junk", ""):
        monkeypatch.setenv(env, bad)
        assert cli._threads_window(None, env, 3.0) == 3.0, (env, bad)


@pytest.mark.parametrize("env", _THREADS_ENVS)
def test_threads_window_rejects_inf_nan_zero_via_flag(monkeypatch, env):
    # argparse gives these knobs type=float, so a bad flag arrives already as a
    # float (inf/nan/<=0) — it must fall back, never leak inf into the window.
    monkeypatch.delenv(env, raising=False)
    for bad in (float("inf"), float("-inf"), float("nan"), 0.0, -1.0):
        assert cli._threads_window(bad, env, 3.0) == 3.0, (env, bad)


def test_threads_window_flag_beats_env_then_default(monkeypatch):
    env = "COORD_THREADS_SILENCE_DAYS"
    monkeypatch.setenv(env, "5")
    assert cli._threads_window(2.0, env, 3.0) == 2.0   # flag wins over env
    assert cli._threads_window(None, env, 3.0) == 5.0  # env wins over default
    monkeypatch.delenv(env, raising=False)
    assert cli._threads_window(None, env, 3.0) == 3.0  # default


# --- retention: opt-in, shared parser, legacy prefix alias-accepted (ENG-2-5/6) ---

def _old_done_task(title="Olddone"):
    # a done task old enough to be archived under any positive-day window
    return (f"---\ntype: Task\ntitle: {title}\nid: {title.lower()}\nstatus: done\n"
            f"timestamp: 2020-01-15T00:00:00Z\n---\nold body")


def _archived(t) -> bool:
    return any("/task/archive/" in k for k in t.store)


def test_retention_on_by_default(monkeypatch):
    monkeypatch.delenv("COORD_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("FULCRA_COORD_RETENTION_DAYS", raising=False)
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    assert cli.main(["reconcile", "r"], transport=t) == 0
    assert _archived(t)


def test_retention_reads_canonical_env(monkeypatch):
    monkeypatch.setenv("COORD_RETENTION_DAYS", "30")
    monkeypatch.delenv("FULCRA_COORD_RETENTION_DAYS", raising=False)
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r"], transport=t)
    assert _archived(t)


def test_retention_accepts_legacy_prefix_alias(monkeypatch):
    """An operator copying old fulcra-coord docs sets FULCRA_COORD_RETENTION_DAYS;
    coord-engine now honors it as a legacy alias (no more silent no-retention)."""
    monkeypatch.delenv("COORD_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("FULCRA_COORD_RETENTION_DAYS", "30")
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r"], transport=t)
    assert _archived(t)


def test_retention_canonical_wins_over_legacy(monkeypatch):
    # canonical set to a disabling value beats a legacy value that would enable
    monkeypatch.setenv("COORD_RETENTION_DAYS", "0")
    monkeypatch.setenv("FULCRA_COORD_RETENTION_DAYS", "30")
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r"], transport=t)
    assert not _archived(t)


def test_retention_flag_overrides_env(monkeypatch):
    # --retention-days wins over the env (constructor/flag precedence)
    monkeypatch.setenv("COORD_RETENTION_DAYS", "0")
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r", "--retention-days", "30"], transport=t)
    assert _archived(t)


@pytest.mark.parametrize("bad", ["nan", "inf", "junk", "-5"])
def test_retention_bad_value_falls_back_to_enabled_default(monkeypatch, bad):
    monkeypatch.setenv("COORD_RETENTION_DAYS", bad)
    monkeypatch.delenv("FULCRA_COORD_RETENTION_DAYS", raising=False)
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r"], transport=t)
    assert _archived(t)


def test_retention_explicit_zero_disables(monkeypatch):
    monkeypatch.setenv("COORD_RETENTION_DAYS", "0")
    t = FakeTransport()
    t.put("team/r/task/olddone.md", _old_done_task())
    cli.main(["reconcile", "r"], transport=t)
    assert not _archived(t)


# --- docs-vs-code: every COORD_* in the README env table is read by the engine ---

_PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent
_README = _PKG_ROOT / "README.md"
_SRC = _PKG_ROOT / "coord_engine"


def _table_env_names() -> set[str]:
    """The `COORD_*` / `FULCRA_COORD_*` names in backticks inside the README's
    'Environment / tuning' section table."""
    text = _README.read_text()
    start = text.index("## Environment")
    section = text[start:]
    return set(re.findall(r"`((?:FULCRA_)?COORD_[A-Z_]+)`", section))


def test_env_table_names_are_read_by_the_code():
    src = "\n".join(p.read_text() for p in _SRC.glob("*.py"))
    documented = _table_env_names()
    assert documented, "the README env table lists no COORD_* names — did it move?"
    missing = sorted(n for n in documented if n not in src)
    assert not missing, f"README env table lists names the code never reads: {missing}"


def test_tuning_knobs_are_documented():
    """The reverse guard: every tuning/budget knob the engine reads is in the table
    (identity/state/log-level vars are covered by their own prose rows too)."""
    documented = _table_env_names()
    for knob in ("COORD_REVIEW_FOLD_BUDGET", "COORD_BRIEFING_BUDGET",
                 "COORD_LISTEN_CLASSIFY_BUDGET", "COORD_OVERLAY_BUDGET",
                 "COORD_OVERLAY_CAP", "COORD_THREADS_FOLD_BUDGET",
                 "COORD_THREADS_SILENCE_DAYS", "COORD_THREADS_INTENT_GRACE_HOURS",
                 "COORD_TRANSPORT_TIMEOUT", "COORD_RETENTION_DAYS"):
        assert knob in documented, f"{knob} is read by the code but absent from the README env table"
