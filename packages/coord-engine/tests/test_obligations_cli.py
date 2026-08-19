"""``coord-engine obligations`` — the wiring, and the false CLEAR it must refuse.

The fold's own rules are tested in ``test_obligations.py``. What matters here is
the binding: whether the real components report UNREADABLE when they cannot be
read. A perfect fold wired to a probe that swallows failure is still a false
CLEAR, and that is the bug this file exists to prevent.

The motivating case is ``reviews``. ``_pending_reviews_for`` is deliberately
best-effort — needs-me and briefing must not fail because a review add-on is down,
so a failed listing returns ``[]``. Read by a fold, that ``[]`` means "no reviews
pending", which is precisely the false clear the slice-3 fold was built to make
impossible. The ``degraded_sink`` parameter is how the fold hears the difference,
and the tests below are what keep it wired.
"""

from __future__ import annotations

import json
import time

import pytest

from coord_engine import cli, obligations as obligations_mod
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

PINNED_NOW = "2026-07-29T22:00:00Z"
TEAM = "fulcra"
AGENT = "opie"


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Repo convention: a module that fixes data timestamps pins the clock."""
    monkeypatch.setattr(cli, "_now", lambda: __import__("datetime").datetime(
        2026, 7, 29, 22, 0, 0, tzinfo=__import__("datetime").timezone.utc))


class ReviewListingDown(FakeTransport):
    """Every read works except the review listing, which times out.

    The exact shape of the 2026-07-20 starvation incident's transport, and the
    one that used to yield a confident empty review section.
    """

    def list_dir(self, prefix: str):
        if prefix == f"team/{TEAM}/review/":
            raise TransportError("review listing timed out")
        return super().list_dir(prefix)


def _fold(transport) -> obligations_mod.ObligationResult:
    return obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)


def test_review_listing_failure_is_unreadable_not_clear():
    """The load-bearing test for the whole slice.

    Without the degraded sink this fold reports CLEAR: the task index is fine, no
    roles are unresolved, and the review helper hands back a tidy empty list. Every
    component says "nothing here" and one of them is guessing.
    """
    result = _fold(ReviewListingDown())

    assert result.state is obligations_mod.ObligationState.UNKNOWN, (
        "a review listing that timed out must make CLEAR unreachable; reporting "
        "clear here is the false-clear this fold exists to refuse"
    )
    assert "reviews" in result.degraded
    assert not result.can_claim_clear
    assert "reviews" in result.reason()


def test_degraded_sink_is_actually_wired_into_the_helper():
    """Non-vacuity: prove the sink fills, not just that the fold went UNKNOWN.

    The fold could report UNKNOWN for unrelated reasons and this suite would look
    fine while the sink was disconnected.
    """
    sink: list[str] = []
    found = cli._pending_reviews_for(
        ReviewListingDown(), TEAM, AGENT, rows=[], degraded_sink=sink)
    assert found == []
    assert sink == ["review-listing"], (
        "the helper must report WHY it returned empty when asked; a silent [] is "
        "indistinguishable from a genuine absence"
    )


def test_best_effort_callers_are_unaffected():
    """needs-me/briefing keep their tidy empty list — no sink, no behavior change.

    The whole point of an opt-in sink: the fold gets to fail closed without
    making every other read surface fragile.
    """
    assert cli._pending_reviews_for(ReviewListingDown(), TEAM, AGENT, rows=[]) == []


def test_healthy_transport_can_reach_clear():
    """The fold must be *able* to say CLEAR, or it is a stuck alarm not a gate."""
    result = _fold(FakeTransport())
    assert result.state is obligations_mod.ObligationState.CLEAR
    assert result.can_claim_clear
    assert result.consulted == sorted(obligations_mod.OBLIGATION_COMPONENTS)


def test_cli_exit_codes_carry_the_terminal_state(capsys):
    """Automation switches on rc, never on prose (r2 spec item 5)."""
    import argparse

    args = argparse.Namespace(team=TEAM, agent=AGENT, json=True)
    rc = cli.cmd_obligations(args, ReviewListingDown())
    assert rc == 3, "UNKNOWN must be a distinct nonzero rc"
    row = json.loads(capsys.readouterr().out)
    assert row["type"] == "obligations"
    assert row["state"] == "UNKNOWN"
    assert "reviews" in row["degraded"]

    args = argparse.Namespace(team=TEAM, agent=AGENT, json=True)
    rc = cli.cmd_obligations(args, FakeTransport())
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "CLEAR"


def test_task_index_failure_degrades_every_row_derived_component():
    """One index read serves four components, so one failure darkens all four.

    Reporting three of them CLEAR because only one was asked about would be
    inventing coverage from a single read.
    """
    class IndexDown(FakeTransport):
        def list_dir(self, prefix: str):
            if prefix.startswith(f"team/{TEAM}/task"):
                raise TransportError("index down")
            return super().list_dir(prefix)

        def read(self, path: str):
            if "/task/" in path or path.endswith("summaries.json"):
                raise TransportError("index down")
            return super().read(path)

    result = _fold(IndexDown())
    assert result.state is obligations_mod.ObligationState.UNKNOWN
    for component in ("blocks", "directives", "reminders", "tasks"):
        assert component in result.degraded, (
            f"{component} derives from the task index and must not report clear "
            "when that index is unreadable"
        )


# --- queue integration (slice 3, item: "fold runs in queue/briefing paths") ---

def test_empty_queue_notice_is_emitted_when_obligations_are_not_checked(capsys):
    """Zero event rows cannot silently masquerade as zero durable work."""
    transport = _queue_transport()
    rc = cli.cmd_queue(_queue_args(), transport)
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOT proof that nothing is owed" in err
    assert "coord-engine obligations" in err


# --- queue --obligations: the switchable half of the integration -------------

def _queue_transport():
    from test_records_write import QueueTransport
    t = QueueTransport(window=[])
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "MomentAnnotation/x",
                      "api_version": "v1alpha1"}))
    return t


def _queue_args(**kw):
    """A queue Namespace whose defaults MIRROR the parser's.

    ``obligations`` defaults False here because that is what argparse hands the
    command; it used to default True, which quietly turned every "default read"
    test in this file into an opt-in read and hid the cost contract behind a
    flag nobody passed (found while fixing round-2 finding 1).
    """
    import argparse
    base = dict(team=TEAM, agent=AGENT, json=False, peek=False, consume=False,
                all=False, obligations=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_opt_out_restores_the_pre_ruling_cost(capsys):
    """--no-obligations must genuinely skip the fold, not merely mute it.

    The ruling kept an opt-out for cost-sensitive callers; an opt-out that still
    pays for three listings is not an opt-out.
    """
    transport = _queue_transport()
    rc = cli.cmd_queue(_queue_args(obligations=False), transport)
    assert rc == 0
    err = capsys.readouterr().err
    assert "queue: obligations " not in err
    assert "NOT proof that nothing is owed" in err


def test_empty_read_does_not_reconcile_unless_asked(capsys, monkeypatch):
    """OPT-IN since 2026-08-02 (promise plan T3(b)): the default wake pays nothing.

    Flipped from ``test_empty_read_reconciles_by_default``. The measured
    grounds are in the plan: at the default budget the fold answers UNKNOWN in
    production every time, so default-ON bought no information at the price of
    3+ listings on every empty wake fleet-wide. The fold, its verb and its
    rc3/rc4 contract are untouched — only the subscription changed.
    """
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    monkeypatch.setattr(
        obligations_mod, "fold",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("the default read must not fold")))
    transport = _queue_transport()
    rc = cli.main(["queue", TEAM, "--agent", AGENT], transport=transport)
    err = capsys.readouterr().err
    assert rc == 0
    assert "queue: obligations " not in err
    assert "NOT proof that nothing is owed" in err


def test_no_obligations_stays_an_accepted_no_op_alias(capsys, monkeypatch):
    """Callers already passing --no-obligations keep working, unchanged."""
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    transport = _queue_transport()
    rc = cli.main(
        ["queue", TEAM, "--agent", AGENT, "--no-obligations"],
        transport=transport)
    assert rc == 0
    err = capsys.readouterr().err
    assert "queue: obligations " not in err
    assert "NOT proof that nothing is owed" in err


def test_obligations_flag_opts_the_empty_read_back_in(capsys, monkeypatch):
    """The opt-in half: asking for the fold still runs it on an empty read."""
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    transport = _queue_transport()
    rc = cli.main(
        ["queue", TEAM, "--agent", AGENT, "--obligations"], transport=transport)
    err = capsys.readouterr().err
    assert rc == 0
    assert "obligations CLEAR" in err


def test_flag_reports_unknown_without_saturating_window_rc(capsys):
    """Fold UNKNOWN is reported, while rc 3 stays reserved for window doubt."""
    from coord_engine.transport import TransportError

    transport = _queue_transport()
    original = transport.list_dir if hasattr(transport, "list_dir") else None

    def dark(prefix):
        if prefix == f"team/{TEAM}/review/":
            raise TransportError("review listing down")
        return original(prefix) if original else []

    transport.list_dir = dark
    rc = cli.cmd_queue(_queue_args(obligations=True, json=True), transport)
    captured = capsys.readouterr()
    row = json.loads(captured.out)
    # 2026-08-01 rc split: fold degradation is a report, not a failure.
    assert rc == 0
    assert row["type"] == "queue-result"
    assert row["obligations"]["state"] == "UNKNOWN"
    assert row["obligations"]["degraded"]


def _eventful_queue_transport():
    from test_records_write import QueueTransport, _event_rec

    transport = QueueTransport(window=[_event_rec("r1", "job", to=AGENT)])
    transport.put(f"team/{TEAM}/_coord/bus-v3/records.json",
                  json.dumps({"data_type": "MomentAnnotation/x",
                              "api_version": "v1alpha1"}))
    return transport


def test_flag_is_honored_even_when_events_were_delivered(capsys):
    """An explicit --obligations always folds — events are not an excuse.

    REVERSED 2026-08-03 (reviewer round-2, findings 1/2). This test used to be
    ``test_flag_does_nothing_when_events_were_delivered`` and pinned the
    opposite: a delivered batch suppressed the fold even when the caller asked
    for it. That is the same defect as finding 2 (a flag accepted and then
    ignored), one path over — the caller who paid for the answer got silence,
    and could not tell that from a genuine CLEAR. The DEFAULT read still folds
    nothing on any window, which is the cost contract that actually matters.
    """
    transport = _eventful_queue_transport()
    rc = cli.cmd_queue(_queue_args(obligations=True), transport)
    captured = capsys.readouterr()
    assert rc == 0
    assert "job" in captured.out
    assert "obligations" in captured.err


def test_delivered_batch_carries_the_real_fold_verdict_under_the_flag(capsys):
    """The machine-readable half: DATA + opt-in reports the fold, not a marker."""
    transport = _eventful_queue_transport()
    rc = cli.cmd_queue(_queue_args(obligations=True, json=True), transport)
    row = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert (row["type"], row["state"], row["count"]) == ("queue-result", "DATA", 1)
    assert row["obligations"]["state"] != "not-checked"
    assert "consulted" in row["obligations"] and "reason" in row["obligations"]


def test_peek_honors_the_flag_and_stays_not_checked_by_default(capsys):
    """`--peek` is a success envelope too, so it obeys the same one rule."""
    transport = _eventful_queue_transport()
    rc = cli.cmd_queue(
        _queue_args(obligations=True, json=True, peek=True), transport)
    row = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert row["cursor"]["advanced"] is False
    assert row["obligations"]["state"] != "not-checked"

    transport = _eventful_queue_transport()
    rc = cli.cmd_queue(
        _queue_args(obligations=False, json=True, peek=True), transport)
    row = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert row["obligations"] == {"state": "not-checked"}


# --- codex-reviewer PR 501: the compositions the focused suite missed --------

def _degraded_marker_transport(marker_type: str):
    """A queue transport whose review fold returns a DEGRADATION MARKER ROW.

    The subtle case: `_pending_reviews_for` signals several incomplete reads with
    marker rows rather than the degraded_sink, so a sink-only check let the marker
    ride through as ordinary owed work — the fold then reported DATA/rc 0 with
    incomplete coverage.
    """
    transport = _queue_transport()
    original = cli._pending_reviews_for

    def patched(*a, **kw):
        return [{"type": marker_type, "scanned": 1, "total": 9},
                {"type": "review-pending", "slug": "pr-777"}]
    return transport, original, patched


@pytest.mark.parametrize("marker", [
    "review-head-degraded",
    "review-fold-degraded",
    "review-orphan-degraded",
    "review-role-degraded",
])
def test_review_degradation_markers_make_the_fold_unknown(monkeypatch, marker):
    """Every review marker degrades the fold, and none of them reach the sink."""
    transport, _orig, patched = _degraded_marker_transport(marker)
    monkeypatch.setattr(cli, "_pending_reviews_for", patched)

    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)

    assert result.state is obligations_mod.ObligationState.UNKNOWN, (
        f"{marker} was treated as ordinary owed work; incomplete review coverage "
        "must make CLEAR/DATA unsayable"
    )
    assert "reviews" in result.degraded
    assert any(r.get("slug") == "pr-777" for r in result.owed), (
        "the pending rows that WERE read must survive the degradation — partial "
        "work stays available while the terminal state stays honest"
    )


def test_forge_degradation_marker_makes_the_fold_unknown(monkeypatch):
    transport = _queue_transport()
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [
        {"type": "forge-degraded", "scanned": 0, "total": 3, "skipped": 3},
    ])
    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    assert result.state is obligations_mod.ObligationState.UNKNOWN
    assert "forge_feedback" in result.degraded


def test_owed_forge_feedback_is_data_not_clear(monkeypatch):
    """The gap the reviewer found: unacked forge feedback IS owed work.

    Before `forge_feedback` joined the registry this fold returned CLEAR while a
    reviewer was waiting — a component nobody named reports nothing and looks
    exactly like one with nothing to report.
    """
    transport = _queue_transport()
    monkeypatch.setattr(cli, "_forge_feedback_for", lambda *a, **kw: [
        {"type": "forge-feedback", "pr_slug": "pr-501", "count": 2},
    ])
    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    assert result.state is obligations_mod.ObligationState.DATA
    assert any(r.get("pr_slug") == "pr-501" for r in result.owed)


@pytest.mark.parametrize("state", ["UNKNOWN", "INVALID"])
def test_queue_json_nests_degraded_fold_in_success_envelope(
        monkeypatch, capsys, state):
    """A clean event window stays successful while naming fold degradation."""
    transport = _queue_transport()

    def fake_fold(*a, **kw):
        return obligations_mod.ObligationResult(
            state=obligations_mod.ObligationState[state],
            degraded=["reviews"] if state == "UNKNOWN" else [],
            malformed=[] if state == "UNKNOWN" else ["tasks"])

    monkeypatch.setattr(obligations_mod, "fold", fake_fold)
    rc = cli.cmd_queue(_queue_args(obligations=True, json=True), transport)
    out = capsys.readouterr().out

    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(rows) == 1, f"--json must emit exactly one object, got {rows}"
    row = rows[0]
    # 2026-08-01 rc split: fold degradation is a report, not a failure.
    assert row["type"] == "queue-result"
    assert row["state"] == "CLEAR"
    assert rc == 0
    assert row["obligations"]["state"] == state, (
        "the diagnosis must survive as a nested field, not be dropped"
    )


# --- codex-coder cost-ack evidence at f48cf81a+ ------------------------------
#
# The re-ack asked for four things, each a test below: op counts at 0/1/several
# responsible PRs, a hard budget that per-PR growth cannot overrun, fail-closed
# on budget expiry with NO false CLEAR, and proof that eventful wakes skip the
# fold while --no-obligations stays byte-exact.

class CountingQueueTransport:
    """Wraps a queue transport and counts every transport operation.

    Counting rather than timing on purpose: op count is the thing that grows
    with an agent's PR count, and it is deterministic. A wall-clock assertion
    would be a flake generator on shared CI.
    """

    def __init__(self, inner, responsible_prs=()):
        self.inner = inner
        self.responsible_prs = list(responsible_prs)
        self.ops = 0
        self.listed: list[str] = []

    def _bump(self, label):
        self.ops += 1
        self.listed.append(label)

    def list_dir(self, prefix):
        self._bump(f"list:{prefix}")
        if prefix.endswith("/_coord/forge/watch/"):
            return [{"name": f"{slug}.md", "is_dir": False}
                    for slug in self.responsible_prs]
        return self.inner.list_dir(prefix) if hasattr(self.inner, "list_dir") else []

    def read(self, path):
        self._bump(f"read:{path}")
        return self.inner.read(path)

    def read_classified(self, path):
        self._bump(f"readc:{path}")
        value = self.inner.read(path)
        return (value, "ok") if value is not None else (None, "absent")

    def records(self, *a, **kw):
        self._bump("records")
        return self.inner.records(*a, **kw)

    def write(self, path, content):
        self._bump(f"write:{path}")
        return self.inner.write(path, content)


def _counting(responsible_prs=()):
    return CountingQueueTransport(_queue_transport(), responsible_prs)


@pytest.mark.parametrize("n_prs", [0, 1, 5])
def test_fold_transport_ops_are_measured_and_reported(n_prs):
    """Op counts at 0 / 1 / several responsible PRs — the evidence asked for.

    This test does not assert a magic number; it asserts the shape the cost-ack
    turns on: the fold's cost is bounded by the budget, and growth with PR count
    is visible rather than hidden. The printed counts are the evidence.
    """
    transport = _counting([f"pr-{i}" for i in range(n_prs)])
    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    print(f"\nresponsible_prs={n_prs} transport_ops={transport.ops}")
    assert result.state in (
        obligations_mod.ObligationState.CLEAR,
        obligations_mod.ObligationState.DATA,
        obligations_mod.ObligationState.UNKNOWN,
    )
    # The bound that matters: the fold never becomes unbounded in PR count
    # without the budget noticing. 200 is far above any real fleet fan-out and
    # far below "runaway"; a breach here means the deadline stopped binding.
    assert transport.ops < 200, (
        f"{transport.ops} transport ops for {n_prs} PRs — the fold's fan-out is "
        "no longer bounded; re-check that the shared deadline is threaded"
    )


def test_expired_budget_is_unknown_never_a_false_clear(monkeypatch):
    """Budget expiry must fail closed.

    The dangerous version of a timeout is the one that returns early with
    whatever it managed to read and calls it complete. With the budget already
    spent the fold must be UNKNOWN — a CLEAR here would be a false clear caused
    by our own cost control, which is the worst possible source for one.

    Time is moved deterministically rather than by setting the budget to zero:
    ``config.env_float`` enforces a STRICT positive floor, so
    ``COORD_OBLIGATION_BUDGET=0`` silently falls back to the default. (That trap
    is worth knowing on its own — an operator setting 0 to disable the fold gets
    20s instead of off.)
    """
    from coord_engine import budget as budget_mod

    clock = {"t": 0.0}

    def fake_monotonic():
        # Time leaps forward on EVERY read, so whichever deadline was opened
        # last is already past by the next check. Keyed to elapsed time rather
        # than to a call count, which silently stopped forcing expiry once the
        # fold opened a second deadline (setup vs probes).
        clock["t"] += 1e9
        return clock["t"]

    monkeypatch.setattr(budget_mod.time, "monotonic", fake_monotonic)
    transport = _counting([f"pr-{i}" for i in range(3)])
    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)
    assert result.state is not obligations_mod.ObligationState.CLEAR, (
        "an exhausted budget produced CLEAR — cost control must never "
        "manufacture a false clear"
    )
    assert result.state is obligations_mod.ObligationState.UNKNOWN
    assert not result.can_claim_clear


def test_the_budget_is_actually_bound_to_the_fold():
    """Non-vacuity: prove the knob exists and the fold reads it.

    Without this, the expiry test above could pass for an unrelated reason and
    the budget could be entirely unthreaded.
    """
    assert cli._obligation_budget() == cli.DEFAULT_OBLIGATION_BUDGET
    import inspect
    src = inspect.getsource(cli._obligation_probes)
    assert "_obligation_budget()" in src
    assert "fold_dl.instant" in src, "the deadline is opened but never passed"
    assert src.count("fold_dl.instant") >= 2, (
        "both transport-heavy probes (reviews, forge) must share the deadline"
    )


@pytest.mark.parametrize("window_events", [0, 1])
def test_default_wake_pays_nothing_for_the_fold(capsys, window_events):
    """A DEFAULT wake must not touch review or forge surfaces — ever.

    Widened 2026-08-03 (round-2 finding 1): this was
    ``test_eventful_wake_pays_nothing_for_the_fold`` and passed
    ``_queue_args()`` back when that helper defaulted ``obligations=True``, so
    it was really asserting "an OPT-IN eventful wake pays nothing" — the flag
    being ignored, which is the defect finding 2 names. The cost contract the
    plan actually bought is the default one, and it holds on both windows.
    """
    from test_records_write import QueueTransport, _event_rec

    window = [_event_rec("r1", "job", to=AGENT)] * window_events
    inner = QueueTransport(window=window)
    inner.put(f"team/{TEAM}/_coord/bus-v3/records.json",
              json.dumps({"data_type": "MomentAnnotation/x",
                          "api_version": "v1alpha1"}))
    transport = CountingQueueTransport(inner)
    cli.cmd_queue(_queue_args(), transport)
    capsys.readouterr()
    touched = [op for op in transport.listed
               if "/review/" in op or "/forge/" in op]
    assert touched == [], (
        f"a default wake paid for the fold: {touched}. The fold is opt-in."
    )


def test_budget_breach_during_the_forge_scan_degrades_not_truncates(monkeypatch):
    """The cap-exceeded case codex-coder asked to see explicitly.

    Distinct from the pre-expired test above: here the fold STARTS with budget
    and runs out partway through the per-PR fan-out. The dangerous outcome is a
    silently short list — some PRs scanned, the rest dropped, reported as
    complete. It must degrade to UNKNOWN instead.
    """
    from coord_engine import budget as budget_mod

    clock = {"t": 0.0}

    def creeping_monotonic():
        # Time creeps one second per read, so a deadline opened with a 5s budget
        # is live when the fan-out starts and breaches partway through it.
        # Deliberately keyed to ELAPSED TIME rather than to a call count: the
        # count version broke the moment the fold opened a second deadline, and
        # it broke by going GREEN (never expiring) rather than red.
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(budget_mod.time, "monotonic", creeping_monotonic)
    monkeypatch.setattr(cli, "_obligation_budget", lambda: 5.0)
    transport = _counting([f"pr-{i}" for i in range(20)])
    result = obligations_mod.fold(
        cli._obligation_probes(transport, TEAM, AGENT, now=PINNED_NOW),
        expected=obligations_mod.OBLIGATION_COMPONENTS)

    assert result.state is not obligations_mod.ObligationState.CLEAR, (
        "a mid-scan budget breach reported CLEAR — a truncated fan-out must "
        "never look like complete coverage"
    )
    assert result.state is obligations_mod.ObligationState.UNKNOWN
    assert result.degraded, "the breach must name which component went dark"


# --- the budget collapse (coord-boss P0, 2026-08-07) ------------------------
#
# `_obligation_probes` opened ONE shared deadline and then spent it on setup:
# measured on the live store, _load_rows_status 6.8s + _held_roles_for_rows
# 19.3s = 26.1s against a 20s budget. Every probe then hit its expired guard
# and returned UNREADABLE WITHOUT TOUCHING THE STORE — 0 of 7 consulted, and
# 110 owed items fleet-wide hidden behind a blanket UNKNOWN. It did not fail to
# read. It never attempted to.

class SlowSetup(FakeTransport):
    """A store whose SETUP reads are slow enough to burn a whole shared budget.

    Only the task-index and role paths are slowed, which is where the real cost
    sits; the per-component probe reads stay fast. That is exactly the live
    profile, so a fold that still collapses here is collapsing for the reason
    the incident did.
    """

    #: The exact two accesses the setup phase makes, verified by instrumenting
    #: the fold. Matched EXACTLY, not by substring: the probes list
    #: ``_coord/forge/watch/``, so a ``_coord/`` prefix test would slow the
    #: probes too and the test would no longer isolate setup cost.
    SETUP_READ = "team/fulcra/_coord/summaries.json"
    SETUP_LIST = "team/fulcra/_coord/"

    def __init__(self, *a, delay=0.05, **kw):
        super().__init__(*a, **kw)
        self._delay = delay

    def list_dir(self, prefix: str):
        if prefix == self.SETUP_LIST:
            time.sleep(self._delay)
        return super().list_dir(prefix)

    def read(self, path: str):
        if path == self.SETUP_READ:
            time.sleep(self._delay)
        return super().read(path)


def test_setup_cost_no_longer_starves_every_probe(monkeypatch):
    """The regression pin. With a budget smaller than setup, the OLD code
    consulted ZERO components. Setup is now accounted separately, so the probes
    still run and the fold reports real coverage."""
    monkeypatch.setattr(cli, "_obligation_budget", lambda: 0.01)
    result = _fold(SlowSetup(delay=0.02))
    assert result.consulted, (
        "every component reported UNREADABLE without touching the store — "
        "setup spent the probes' budget, which is the 2026-08-07 collapse"
    )


def test_a_degraded_component_says_WHY_not_just_which(monkeypatch):
    """coord-boss: 'a degraded report that will not say why is one you cannot
    act on.' An exhausted budget and an unreachable store rendered identically,
    and their remedies are opposite."""
    monkeypatch.setattr(cli, "_obligation_budget", lambda: 0.01)
    result = _fold(SlowSetup(delay=0.02))
    if not result.degraded:                     # fast machine: force the case
        result = obligations_mod.fold(
            [obligations_mod.Component(
                name="reviews",
                probe=lambda: obligations_mod.ProbeResult(
                    state=obligations_mod.ProbeState.UNREADABLE,
                    detail="obligation probe budget exhausted"))],
            expected=("reviews",))
    for name in result.degraded:
        assert result.details.get(name), f"{name} degraded with no reason given"
        assert name in result.reason() and result.details[name] in result.reason()


def test_review_listing_failure_still_reports_its_transport_reason():
    """The other half: a real transport failure must NOT be described as a
    budget problem. The two causes stay distinguishable in the output."""
    result = _fold(ReviewListingDown())
    assert "reviews" in result.degraded
    why = result.details.get("reviews", "")
    assert why, "a transport failure must carry its reason"
    assert "budget" not in why.lower(), (
        "a transport failure described as a budget problem sends the reader to "
        "the wrong remedy"
    )


# --- the raised default (coord-boss ruling, 2026-08-07) ---------------------

def test_default_probe_budget_is_the_measured_floor_not_the_old_collapse_value():
    """Pins the raise, and pins WHY.

    At 20 the fold was measured blind on three hosts (0/7, 0/7, 1/7 consulted)
    with 110 / 64 / 6 owed items hidden. 90 is the smallest value observed to
    help. The assertion is a floor rather than equality so the follow-up cost
    fix can lower the real cost without this test dictating the number -- but
    it can never silently slide back toward the value that collapsed.
    """
    assert cli.DEFAULT_OBLIGATION_BUDGET >= 90.0, (
        "the default probe budget was measured blind at 20s on three hosts; "
        "lowering it below the observed floor reinstates that collapse"
    )


def test_the_default_does_not_claim_to_be_sufficient():
    """coord-boss: 90 must not become the number everyone assumes is enough.

    `role_duties` still reports UNREADABLE at 90 on at least one host, so the
    constant carries that caveat in prose. A future editor who raises or lowers
    the number without reading it is exactly who this test is for.
    """
    import inspect
    src = inspect.getsource(cli)
    marker = src[src.find("#: Deadline (seconds) for the obligation fold's PROBES"):
                 src.find("DEFAULT_OBLIGATION_BUDGET = ")]
    assert "floor, not a proven ceiling" in marker
    assert "role_duties" in marker, (
        "the known-still-blind component must stay named at the constant"
    )


# --- the measured deadline must BIND (coord-boss correction, 2026-08-07) ----
#
# 555 opened setup_dl from _obligation_budget() and then called
# _held_roles_for_rows WITHOUT deadline_seconds, so the role fold fell through
# to COORD_ROLE_FOLD_BUDGET. The deadline that was MEASURED was not the
# deadline that BOUND the work -- and the warning told operators to raise a
# variable that governs something else. Three people acted on that message,
# one of them in a fleet-wide ruling.

def test_the_obligations_setup_deadline_actually_binds_the_role_fold(monkeypatch):
    """The regression pin: the obligation budget must reach the role resolver.

    Asserts the VALUE passed, not merely that a keyword was supplied -- a call
    that passed the role fold's own default would satisfy a weaker check while
    reinstating exactly the bug.
    """
    seen = {}
    real = cli._held_roles_for_rows

    def spy(*a, **kw):
        seen["deadline_seconds"] = kw.get("deadline_seconds")
        return real(*a, **kw)

    monkeypatch.setattr(cli, "_held_roles_for_rows", spy)
    monkeypatch.setattr(cli, "_obligation_budget", lambda: 7.0)
    monkeypatch.setattr(cli, "_role_fold_budget", lambda: 999.0)
    cli._obligation_probes(FakeTransport(), TEAM, AGENT, now=PINNED_NOW)

    got = seen["deadline_seconds"]
    assert got is not None, "the role fold ran unbounded by the obligation budget"
    assert got <= 7.0, f"bound by something other than the obligation budget: {got}"
    assert got != 999.0, "fell through to COORD_ROLE_FOLD_BUDGET — the 555 bug"


def test_every_production_callsite_passes_the_budget_explicitly():
    """coord-boss: 'a parameter only tests reach does not exist in production.'

    Asserts the INVARIANT — no production call omits `deadline_seconds` — rather
    than counting literal spellings. The string-counting version broke the
    moment briefing started COMPOSING two budgets (min(own cap, shared window)),
    which is a stronger callsite than the one the test was written against. A
    test that fails when a callsite improves is measuring the wrong thing.
    """
    import inspect
    src = inspect.getsource(cli)
    needle = "_held_roles_for_rows("
    unbound = []
    for i in range(len(src)):
        if not src.startswith(needle, i):
            continue
        if src.rfind("def ", 0, i) == i - 4:      # the definition itself
            continue
        depth, j = 0, i + len(needle) - 1
        while j < len(src):                        # walk to the matching paren
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        call = src[i:j]
        if "deadline_seconds" not in call:
            unbound.append(src[max(0, i - 120):i].splitlines()[-1].strip())
    assert not unbound, (
        "these _held_roles_for_rows callsites state no budget, so the role fold "
        f"runs on a hidden default: {unbound}"
    )


def test_remaining_never_reports_a_spent_deadline_as_unlimited():
    """`remaining()` on a spent deadline must be 0.0, not None and not negative.

    None means unbounded to every callee, so returning it here would turn an
    exhausted budget into an unlimited one — the fail-open this whole family of
    bugs keeps producing.
    """
    from coord_engine.budget import Deadline
    spent = Deadline.open(0.0)
    assert spent.remaining() == 0.0
    assert Deadline(None).remaining() is None
