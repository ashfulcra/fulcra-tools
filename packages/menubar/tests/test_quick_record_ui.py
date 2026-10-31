"""Actual view closures with fake widgets and queued worker/main callbacks.

No AppKit, credentials or network. These exercise draft preservation, durable
capture, account-switch ordering and proof checks in the actual view function.
"""

from __future__ import annotations
import ast
from datetime import datetime, timezone, timedelta
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID, uuid4

from fulcra_collect.record_actions import ActionJournal
from fulcra_menubar._humanize import parse_duration_seconds
from fulcra_menubar.quick_record_client import QuickRecordClient, _scope
from fulcra_menubar.quick_record_requests import ClientRequestJournal

REPO = Path(__file__).resolve().parents[3]


def u(number):
    return str(UUID(int=number))


A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
B = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
DEST = "Event/11111111-2222-4333-8444-555555555555"


def scope(account=A, epoch="captured"):
    return dict(protocol_version=2, account_id=account, config_epoch=epoch)


def view(account=A, epoch="captured"):
    return dict(
        scope(account, epoch),
        ok=True,
        favorites=[u(2), u(3)],
        favorites_revision=1,
        definitions=[
            dict(
                id=u(2),
                name="Synthetic moment",
                annotation_type="moment",
                digest="a" * 64,
                pinned=True,
            ),
            dict(
                id=u(3),
                name="Synthetic duration",
                annotation_type="duration",
                digest="b" * 64,
                pinned=True,
            ),
        ],
        recent=[],
        timers=[
            dict(
                timer_id=u(10),
                captured_epoch=epoch,
                definition_id=u(3),
                status="running",
                name="Synthetic timer",
                start_time="2026-01-02T12:00:00Z",
                end_time=None,
                action_id=None,
            )
        ],
    )


class Widget:
    def __init__(self):
        self.children = []
        self.value = ""
        self.parent = None
        self.editable = False

    @classmethod
    def alloc(cls):
        return cls()

    @classmethod
    def labelWithString_(cls, s):
        x = cls()
        x.value = s
        return x

    def initWithFrame_(self, *_):
        return self

    def addSubview_(self, x):
        self.children.append(x)
        x.parent = self

    def subviews(self):
        return self.children

    def removeFromSuperview(self):
        self.parent.children.remove(self)
        self.parent = None

    def setStringValue_(self, s):
        self.value = s

    def stringValue(self):
        return self.value

    def setDocumentView_(self, v):
        self.doc = v

    def layer(self):
        return self

    def refresh(self):
        self._refresh_callback()

    def isEditable(self):
        return self.editable

    def __getattr__(self, n):
        return lambda *a: None


class Button(Widget):
    pass


class Field(Widget):
    def initWithFrame_(self, *_):
        self.editable = True
        return self


def moment_row(*args, **kwargs):
    row = Widget()
    row.addSubview_(Field().initWithFrame_())
    return row


def duration_row(*args, **kwargs):
    row = moment_row()
    row.addSubview_(Field().initWithFrame_())
    return row


class StripImports(ast.NodeTransformer):
    def visit_Import(self, node):
        return None

    def visit_ImportFrom(self, node):
        return None


class Harness:
    def __init__(self, initial=None):
        self.threads = []
        self.main = []
        self.applied = []
        self.temp = TemporaryDirectory(prefix="collect-native-ui-test-")
        self.journal = ClientRequestJournal(
            ActionJournal(Path(self.temp.name) / "client.sqlite")
        )
        self.current = initial or view()
        self.response = None
        self.calls = []
        self.workflow = QuickRecordClient(self, self.journal)
        owner = self

        class QueuedThread:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                owner.threads.append(self.target)

        def dispatch(fn):
            def queue(*a, **kw):
                owner.main.append(lambda: fn(*a, **kw))

            return queue

        path = REPO / "packages/menubar/fulcra_menubar/popover/quick_record_v2.py"
        outer = next(
            n
            for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "make_view"
        )
        module = StripImports().visit(ast.Module(body=[outer], type_ignores=[]))
        ast.fix_missing_locations(module)
        env = dict(
            detach_view=lambda view: None,
            refresh_view=lambda frame: Widget().initWithFrame_(frame),
            NSView=Widget,
            NSScrollView=Widget,
            NSButton=Button,
            NSTextField=Field,
            NSBezelStyleRounded=0,
            NSMakeRect=lambda *a: a,
            _FlippedView=Widget,
            Thread=QueuedThread,
            on_main_thread=dispatch,
            native_client=lambda _: self.workflow,
            colors=SimpleNamespace(
                bg=lambda: Widget(), text_secondary=lambda: Widget()
            ),
            typography=SimpleNamespace(small=lambda: None),
            attach=lambda w, f: setattr(w, "callback", f),
            _make_section_header=lambda *a: Widget(),
            _make_moment_row=moment_row,
            _make_duration_row=duration_row,
            quick_record_view_state=lambda choices: (
                choices,
                "no_defs" if not choices else None,
            ),
            _group_label=lambda kind: kind,
            _scope=_scope,
            datetime=datetime,
            timezone=timezone,
            timedelta=timedelta,
            uuid4=uuid4,
            parse_duration_seconds=parse_duration_seconds,
        )
        exec(compile(module, str(path), "exec"), env)
        self.ui = env["make_view"](
            client=self,
            model=None,
            on_view_status=lambda: None,
            width=360,
            on_timer_changed=lambda: (
                self.applied.append(self.state["snapshot"]["account_id"])
                if hasattr(self, "state") and self.state["snapshot"]
                else None
            ),
        )
        refresh_vars = inspect.getclosurevars(self.ui._refresh_callback).nonlocals
        self.state = refresh_vars["state"]
        self.worker = refresh_vars["worker"]
        self.render = inspect.getclosurevars(self.worker).nonlocals["render"]
        self.rvars = inspect.getclosurevars(self.render).nonlocals

    def quick_record_v2(self, operation, **fields):
        self.calls.append((operation, fields))
        if operation == "quick_record_list":
            return self.current
        if self.response is not None:
            return self.response
        raise TimeoutError("Synthetic unavailable response")

    def run_thread(self):
        self.threads.pop(0)()

    def run_main(self):
        self.main.pop(0)()

    def drain(self):
        for _ in range(30):
            if self.threads:
                self.run_thread()
            elif self.main:
                self.run_main()
            else:
                return
        raise AssertionError("unexpected unbounded work")

    def widgets(self, key, account=A, epoch="captured"):
        return self.rvars["draft_widgets"][(account, epoch, *key)]

    def send(self, *args, **kwargs):
        return self.rvars["send"](*args, **kwargs)

    def close(self):
        self.temp.cleanup()


def test_drafts_survive_unrelated_worker_and_account_scopes():
    h = Harness()
    try:
        h.drain()
        h.widgets(("choice", u(2)))[0].setStringValue_("Synthetic moment note")
        h.widgets(("choice", u(3)))[0].setStringValue_("Synthetic duration note")
        h.widgets(("choice", u(3)))[1].setStringValue_("90m")
        h.widgets(("timer", u(10)))[0].setStringValue_("Synthetic stop note")
        h.worker(lambda: None, lambda result, error: None)
        assert h.widgets(("timer", u(10)))[0].value == "Synthetic stop note"
        assert h.widgets(("choice", u(2)))[0].value == "Synthetic moment note"
        assert [f.value for f in h.widgets(("choice", u(3)))] == [
            "Synthetic duration note",
            "90m",
        ]
        h.drain()
        h.ui.refresh()
        h.drain()
        assert h.widgets(("timer", u(10)))[0].value == "Synthetic stop note"
        h.current = view(B, "other")
        h.ui.refresh()
        h.drain()
        assert h.widgets(("timer", u(10)), B, "other")[0].value == ""
        h.current = view()
        h.ui.refresh()
        h.drain()
        assert h.widgets(("timer", u(10)))[0].value == "Synthetic stop note"
    finally:
        h.close()


def test_capture_clears_only_its_own_draft_after_durability():
    h = Harness()
    try:
        h.drain()
        h.widgets(("choice", u(2)))[0].setStringValue_("Synthetic captured note")
        h.widgets(("timer", u(10)))[0].setStringValue_("Synthetic retained timer note")
        assert h.send(
            h.current,
            "record_annotation",
            draft_key=("choice", u(2)),
            action_id=u(101),
            definition_id=u(2),
            choice_digest="a" * 64,
            start_time="2026-01-02T12:00:00Z",
            comment="Synthetic captured note",
        )
        saved = h.journal.get(u(101), account_id=A)
        assert (
            saved.status == "queued"
            and saved.body["comment"] == "Synthetic captured note"
        )
        assert h.widgets(("choice", u(2)))[0].value == ""
        assert h.widgets(("timer", u(10)))[0].value == "Synthetic retained timer note"
        h.drain()
        h.widgets(("choice", u(2)))[0].setStringValue_("Synthetic failed capture note")
        assert not h.send(
            h.current,
            "record_annotation",
            draft_key=("choice", u(2)),
            action_id=u(101),
            definition_id=u(2),
            choice_digest="a" * 64,
            start_time="2026-01-02T12:00:00Z",
            comment="Changed forbidden body",
        )
        assert h.widgets(("choice", u(2)))[0].value == "Synthetic failed capture note"
    finally:
        h.close()


def test_queued_old_account_result_is_discarded_and_refresh_deferred():
    h = Harness()
    try:
        h.run_thread()  # A result now queued for main-thread delivery.
        h.current = view(B, "other")
        h.ui.refresh()
        assert h.state["refresh_pending"]
        h.run_main()
        assert h.state["snapshot"] is None and h.threads
        h.drain()
        assert h.state["snapshot"]["account_id"] == B
        assert h.applied == [B]
    finally:
        h.close()


def test_invalid_reconcile_cannot_ack_ui_or_capture_new_intent():
    for changes in [dict(ok=False), dict(action_id=u(999))]:
        h = Harness()
        try:
            h.drain()
            observed = dict(
                scope(),
                ok=False,
                accepted=True,
                status="submitted",
                action_id=u(201),
                operation="record",
                captured_epoch="captured",
                record_id=u(202),
                data_type=DEST,
            )
            h.response = (
                dict(observed, ok=True, status="verified", **changes)
                if "ok" not in changes
                else dict(observed, status="verified", **changes)
            )
            h.rvars["reconcile"](h.current, action=observed)
            h.run_thread()
            h.run_main()
            assert h.state["message"].startswith("Request retained.")
            assert h.journal.pending(account_id=A)["requests"] == ()
            assert h.calls[-1][0] == "quick_record_reconcile"
        finally:
            h.close()


def test_timer_reconcile_proof_and_old_epoch_guard():
    h = Harness()
    try:
        h.drain()
        timer = dict(
            h.current["timers"][0],
            status="stopping",
            action_id=u(301),
            end_time="2026-01-02T13:00:00Z",
        )
        receipt = dict(
            scope(),
            ok=True,
            accepted=True,
            status="verified",
            action_id=u(301),
            operation="record",
            captured_epoch="captured",
            record_id=u(302),
            data_type=DEST,
            timer=dict(timer, status="verified"),
        )
        h.response = receipt
        assert h.workflow.reconcile(view=h.current, timer=timer)["status"] == "verified"
        for field, value in [
            ("timer_id", u(999)),
            ("action_id", u(999)),
            ("definition_id", u(999)),
            ("start_time", "2026-01-02T11:00:00Z"),
            ("end_time", "2026-01-02T14:00:00Z"),
            ("status", "stopping"),
            ("captured_epoch", "other"),
        ]:
            h.response = dict(receipt, timer=dict(receipt["timer"], **{field: value}))
            try:
                h.workflow.reconcile(view=h.current, timer=timer)
            except ValueError:
                pass
            else:
                raise AssertionError("changed timer proof accepted: " + field)
        count = len(h.calls)
        try:
            h.workflow.reconcile(
                view=h.current, timer=dict(timer, captured_epoch="old")
            )
        except ValueError:
            pass
        else:
            raise AssertionError("old epoch recovery authorized")
        assert len(h.calls) == count
        assert h.journal.pending(account_id=A)["requests"] == ()
    finally:
        h.close()


def test_invalid_or_overflowing_duration_preserves_draft_and_creates_no_request():
    h = Harness()
    try:
        h.drain()
        note = h.widgets(("choice", u(3)))[0]
        note.setStringValue_("Synthetic unsaved duration note")
        for text in ("9" * 400 + "h", "9" * 5000):
            assert not h.rvars["record"](h.current, u(3), "Synthetic duration", note.value, text)
            assert not h.threads
            assert h.journal.pending(account_id=A)["requests"] == ()
            assert note.value == "Synthetic unsaved duration note"
    finally:
        h.close()
