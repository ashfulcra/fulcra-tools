"""Single-write retry + verify-after-write (the silent single-write loss fix).

WHY THIS EXISTS — live evidence, 2026-06-10, four losses in one evening: under
backend write-throttling, single task/directive writes (tell / later / done)
intermittently failed AFTER the CLI printed success-looking output. The body
never landed on the bus, the local cache held it, and it self-healed only at a
much later reconcile — senders believed messages were delivered; recipients
never saw them (a review-queue directive, a `later` backlog item, two loop
closes). The reconcile-pool retry (FULCRA_COORD_UPLOAD_RETRY) covers VIEW
uploads only; the AUTHORITATIVE task-body upload inside
``writepipe._write_task_and_views`` had a single attempt and — worse — an
upload could return ambiguous success with nothing on the bus.

These tests pin the two new behaviours on the task-body upload ONLY (the
best-effort view/event/directive side-writes are untouched):

1. RETRY: a False/raising body upload is retried ONCE after a 0.5–2.0s jitter
   sleep (``FULCRA_COORD_WRITE_RETRY``, default 1, ``0`` disables). A second
   failure falls through to today's exact cached-locally path (return False,
   marker failed + needs_reconcile).
2. VERIFY: after a successful-looking upload, the existing post-write
   ``remote.stat`` doubles as delivery verification when
   ``FULCRA_COORD_WRITE_VERIFY`` is on (default 1). A None stat (file not
   visible on the bus) triggers one more re-upload + re-stat; if STILL
   unverified, an UNMISSABLE ``DELIVERY NOT CONFIRMED: <task-id>`` warning is
   emitted and the op marker is left needs_reconcile — but the write still
   returns True (the self-heal contract stands: cached locally, repaired at the
   next reconcile; the sender's exit code must not flip).

The successful upload path still verifies with one post-upload stat —
verification reuses the version-tracking stat, it does not add another
round-trip after the upload. Confirmed-new writes may spend an extra pre-upload
stat to disambiguate absence from a read failure.
"""

import io as _io
import contextlib
from types import SimpleNamespace

from fulcra_coord import cache, cli, remote, schema, writepipe


# ---------------------------------------------------------------------------
# harness: intercept the TASK-BODY upload/stat only, delegate everything else
# (views, event shards, summaries) to the real fake backend so the rest of the
# pipeline runs for real.
# ---------------------------------------------------------------------------

class _BodyScript:
    """Scripted outcomes for remote.upload_json / remote.stat on ONE task path.

    ``upload_results`` / ``stat_results`` are consumed per call against the
    task path; when a list is exhausted the LAST element repeats (so "always
    absent" is just ``[None]``). Calls for any other path delegate to the real
    functions. ``stat_calls_after_upload`` counts task-path stats issued after
    the first body-upload attempt — the verification-cost observable (the
    pre-write optimistic-concurrency stat is excluded by design).
    """

    def __init__(self, monkeypatch, task_path, *, upload_results, stat_results):
        self.task_path = task_path
        self.upload_results = list(upload_results)
        self.stat_results = list(stat_results)
        self.upload_calls = 0
        self.stat_calls = 0
        self.stat_calls_after_upload = 0
        self.sleeps = []

        real_upload = remote.upload_json
        real_stat = remote.stat

        def fake_upload(data, path, **kw):
            if path == self.task_path:
                self.upload_calls += 1
                result = (self.upload_results.pop(0)
                          if len(self.upload_results) > 1
                          else self.upload_results[0])
                if isinstance(result, Exception):
                    raise result
                return result
            return real_upload(data, path, **kw)

        def fake_stat(path, **kw):
            if path == self.task_path:
                self.stat_calls += 1
                if self.upload_calls:
                    self.stat_calls_after_upload += 1
                return (self.stat_results.pop(0)
                        if len(self.stat_results) > 1
                        else self.stat_results[0])
            return real_stat(path, **kw)

        monkeypatch.setattr(remote, "upload_json", fake_upload)
        monkeypatch.setattr(remote, "stat", fake_stat)
        monkeypatch.setattr(writepipe, "_retry_sleep", self.sleeps.append)


_OK_STAT = {"size": 123, "modified": "2026-06-10T00:00:00Z"}


def _make_task():
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    return t


def _write(task, backend):
    """Run the write capturing stderr (where warn() lines land)."""
    buf = _io.StringIO()
    with contextlib.redirect_stderr(buf):
        ok = writepipe._write_task_and_views(task, backend=backend, command="update")
    return ok, buf.getvalue()


# ---------------------------------------------------------------------------
# 1. fail once -> retried once with jitter -> success, no warn
# ---------------------------------------------------------------------------

def test_body_upload_fail_once_recovers_on_retry(coord_backend, monkeypatch):
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[False, True],          # transient throttle, then lands
        stat_results=[None, None, _OK_STAT],   # pre-stat, confirm absent, verify
    )
    ok, err = _write(t, coord_backend)
    assert ok is True
    assert script.upload_calls == 2, "body must be uploaded twice (1 retry)"
    assert "DELIVERY NOT CONFIRMED" not in err
    # The retry slept once, with jitter inside the contract window.
    assert len(script.sleeps) == 1
    assert 0.5 <= script.sleeps[0] <= 2.0


def test_body_upload_raise_once_recovers_on_retry(coord_backend, monkeypatch):
    """A RAISING upload is a failure, not an escape hatch — it must be retried,
    not propagate out and bypass the cached-locally contract."""
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[RuntimeError("transport blew up"), True],
        stat_results=[None, None, _OK_STAT],
    )
    ok, err = _write(t, coord_backend)
    assert ok is True
    assert script.upload_calls == 2
    assert "DELIVERY NOT CONFIRMED" not in err


# ---------------------------------------------------------------------------
# 2. fail twice -> today's exact cached-locally failure path, unchanged
# ---------------------------------------------------------------------------

def test_body_upload_fail_twice_keeps_cached_locally_path(coord_backend, monkeypatch):
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[False],                # always fails
        stat_results=[None],
    )
    ok, _err = _write(t, coord_backend)
    assert ok is False, "second failure must return False (caller warns cached-locally)"
    assert script.upload_calls == 2, "exactly one retry, then give up"
    # Marker semantics unchanged: failed + needs_reconcile survives for self-heal.
    markers = cache.list_op_markers()
    assert any(
        m.get("task_id") == t["id"]
        and m.get("status") == "failed"
        and m.get("needs_reconcile")
        for m in markers
    ), f"expected a failed/needs_reconcile marker, got {markers}"


# ---------------------------------------------------------------------------
# 3. verify finds the write missing -> re-upload + UNMISSABLE warn, still True
# ---------------------------------------------------------------------------

def test_verify_missing_write_warns_delivery_not_confirmed(coord_backend, monkeypatch):
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[True],                 # upload always CLAIMS success
        stat_results=[None],                   # ...but the bus never shows the file
    )
    ok, err = _write(t, coord_backend)
    # Self-heal contract: cached locally, exit code must NOT flip.
    assert ok is True
    assert "DELIVERY NOT CONFIRMED" in err
    assert t["id"] in err
    assert script.upload_calls == 2, "verify failure must re-upload once"
    # The unverified op marker survives for reconcile to repair.
    markers = cache.list_op_markers()
    assert any(
        m.get("task_id") == t["id"] and m.get("needs_reconcile")
        for m in markers
    ), f"expected a needs_reconcile marker for the unverified write, got {markers}"
    # And the body is cached locally (the self-heal source).
    assert cache.read_cached_task(t["id"]) is not None


def test_reconcile_replays_unverified_cached_task_body(coord_backend, monkeypatch):
    """An unverified task-body write is not fixed by a view rebuild alone.

    Regression: the first implementation left a needs_reconcile marker but
    reconcile never replayed the cached task body. It would rebuild/clear views
    from remote state and could drop the very task it promised to self-heal.
    """
    t = _make_task()
    task_path = remote.task_remote_path(t["id"])
    real_upload = remote.upload_json
    real_stat = remote.stat

    def upload_claims_success_without_writing(data, path, **kw):
        if path == task_path:
            return True
        return real_upload(data, path, **kw)

    def task_body_never_visible(path, **kw):
        if path == task_path:
            return None
        return real_stat(path, **kw)

    monkeypatch.setattr(remote, "upload_json", upload_claims_success_without_writing)
    monkeypatch.setattr(remote, "stat", task_body_never_visible)
    monkeypatch.setattr(writepipe, "_retry_sleep", lambda seconds: None)

    ok, err = _write(t, coord_backend)
    assert ok is True
    assert "DELIVERY NOT CONFIRMED" in err
    assert remote.download_json(task_path, backend=coord_backend) is None
    assert any(
        m.get("task_id") == t["id"]
        and m.get("status") == "unverified"
        and m.get("needs_reconcile")
        for m in cache.list_op_markers()
    )

    monkeypatch.setattr(remote, "upload_json", real_upload)
    monkeypatch.setattr(remote, "stat", real_stat)

    assert cli.cmd_reconcile(SimpleNamespace(), backend=coord_backend) == 0
    assert remote.download_json(task_path, backend=coord_backend)["id"] == t["id"]
    assert not [
        m for m in cache.list_op_markers()
        if m.get("task_id") == t["id"] and m.get("needs_reconcile")
    ]


def test_verify_recovers_on_reupload_no_warn(coord_backend, monkeypatch):
    """First post-stat misses, the re-upload + re-stat verifies -> quiet success."""
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        # pre-stat None, confirm absent, post-stat None (unverified), re-stat OK
        upload_results=[True],
        stat_results=[None, None, None, _OK_STAT],
    )
    ok, err = _write(t, coord_backend)
    assert ok is True
    assert "DELIVERY NOT CONFIRMED" not in err
    assert script.upload_calls == 2


# ---------------------------------------------------------------------------
# 4. env knobs
# ---------------------------------------------------------------------------

def test_write_retry_knob_zero_disables_retry(coord_backend, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_WRITE_RETRY", "0")
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[False],
        stat_results=[None],
    )
    ok, _err = _write(t, coord_backend)
    assert ok is False
    assert script.upload_calls == 1, "retry disabled -> single attempt"
    assert script.sleeps == []


def test_write_verify_knob_zero_disables_verification(coord_backend, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_WRITE_VERIFY", "0")
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[True],
        stat_results=[None],                   # stat never sees it — but verify is off
    )
    ok, err = _write(t, coord_backend)
    assert ok is True
    assert "DELIVERY NOT CONFIRMED" not in err
    assert script.upload_calls == 1, "verify off -> no verification re-upload"


# ---------------------------------------------------------------------------
# 5. upload fast path: success + verified costs exactly 1 post-upload stat
# ---------------------------------------------------------------------------

def test_fast_path_costs_one_upload_one_verify_stat(coord_backend, monkeypatch):
    t = _make_task()
    script = _BodyScript(
        monkeypatch, remote.task_remote_path(t["id"]),
        upload_results=[True],
        stat_results=[None, None, _OK_STAT],   # pre-stat, confirm absent, verify
    )
    ok, err = _write(t, coord_backend)
    assert ok is True
    assert "DELIVERY NOT CONFIRMED" not in err
    assert script.upload_calls == 1
    # Verification reuses the version-tracking post-stat: exactly ONE task-path
    # stat after the upload, no extra verification round-trip.
    assert script.stat_calls_after_upload == 1
    assert script.sleeps == []
