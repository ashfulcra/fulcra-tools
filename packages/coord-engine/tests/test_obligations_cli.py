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

def test_empty_queue_notice_is_proposed_but_not_wired():
    """The notice exists as a reviewed string, deliberately not emitted.

    Wiring it would break slice 4's golden contract, which pins text-mode CLEAR
    stderr byte-for-byte. That contract is another agent's, freshly merged and
    deliberately pinned, so changing it is a decision to request rather than a
    constant to bump. This test records the state so the proposal cannot rot into
    a half-applied change nobody remembers.
    """
    assert "NOT proof that nothing is owed" in cli._QUEUE_EMPTY_IS_NOT_CLEAR
    assert "coord-engine obligations" in cli._QUEUE_EMPTY_IS_NOT_CLEAR
    source = __import__("inspect").getsource(cli.cmd_queue)
    assert "_QUEUE_EMPTY_IS_NOT_CLEAR" not in source, (
        "the notice is wired into cmd_queue; slice 4's golden CLEAR test must be "
        "updated in the same change, with coord-boss/codex-coder agreement"
    )


# --- queue --obligations: the switchable half of the integration -------------

def _queue_transport():
    from test_records_write import QueueTransport
    t = QueueTransport(window=[])
    t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
          json.dumps({"data_type": "MomentAnnotation/x",
                      "api_version": "v1alpha1"}))
    return t


def _queue_args(**kw):
    import argparse
    base = dict(team=TEAM, agent=AGENT, json=False, peek=False, consume=False,
                all=False, obligations=True)
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
    assert "obligations" not in capsys.readouterr().err


def test_empty_read_reconciles_by_default(capsys):
    """Default ON: a plain empty wake now reconciles without being asked."""
    transport = _queue_transport()
    rc = cli.cmd_queue(_queue_args(), transport)
    err = capsys.readouterr().err
    assert "obligations CLEAR" in err
    assert rc == 0


def test_flag_surfaces_unknown_in_the_exit_code(capsys):
    """A degraded fold must reach rc, or a scripted wake learns nothing from it."""
    from coord_engine.transport import TransportError

    transport = _queue_transport()
    original = transport.list_dir if hasattr(transport, "list_dir") else None

    def dark(prefix):
        if prefix == f"team/{TEAM}/review/":
            raise TransportError("review listing down")
        return original(prefix) if original else []

    transport.list_dir = dark
    rc = cli.cmd_queue(_queue_args(obligations=True), transport)
    err = capsys.readouterr().err
    assert rc == 3, "UNKNOWN must be a distinct nonzero rc, not a printed aside"
    assert "obligations UNKNOWN" in err
    assert "reviews" in err


def test_flag_does_nothing_when_events_were_delivered(capsys):
    """Reconciliation is for the empty case; a delivered batch is already work."""
    from test_records_write import QueueTransport, _event_rec

    transport = QueueTransport(window=[_event_rec("r1", "job", to=AGENT)])
    transport.put(f"team/{TEAM}/_coord/bus-v3/records.json",
                  json.dumps({"data_type": "MomentAnnotation/x",
                              "api_version": "v1alpha1"}))
    rc = cli.cmd_queue(_queue_args(obligations=True), transport)
    captured = capsys.readouterr()
    assert rc == 0
    assert "job" in captured.out
    assert "obligations" not in captured.err


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


@pytest.mark.parametrize("state,error_code", [
    ("UNKNOWN", "obligations-unknown"),
    ("INVALID", "obligations-invalid"),
])
def test_queue_json_emits_one_queue_error_on_a_degraded_fold(
        monkeypatch, capsys, state, error_code):
    """A degraded fold is a FAILED queue exit — one queue-error, queue's rc 3.

    The bug: queue printed the SUCCESS envelope (`queue-result`, state CLEAR) and
    merely returned nonzero, so automation switching on `type` read a clean CLEAR
    while the process signalled failure.
    """
    transport = _queue_transport()

    def fake_fold(*a, **kw):
        return obligations_mod.ObligationResult(
            state=obligations_mod.ObligationState[state],
            degraded=["reviews"] if state == "UNKNOWN" else [],
            malformed=[] if state == "UNKNOWN" else ["tasks"])

    monkeypatch.setattr(obligations_mod, "fold", fake_fold)
    rc = cli.cmd_queue(_queue_args(json=True), transport)
    out = capsys.readouterr().out

    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(rows) == 1, f"--json must emit exactly one object, got {rows}"
    row = rows[0]
    assert row["type"] == "queue-error", (
        "a degraded fold emitted the success envelope; slice 4's contract is that "
        "every nonzero queue exit is one queue-error object"
    )
    assert row["state"] == state
    assert row["error_code"] == error_code
    assert row["rc"] == 3
    assert rc == 3, "queue keeps rc 3 for both UNKNOWN and INVALID"
    assert row["obligations"]["state"] == state, (
        "the diagnosis must survive as a nested field, not be dropped"
    )
