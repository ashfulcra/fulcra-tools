"""Private standalone receiver ownership; request threads never hold authority."""
from copy import deepcopy
from dataclasses import dataclass, field
import queue
import sys
import threading


class OwnerClosed(RuntimeError):
    pass


class OwnerFailed(RuntimeError):
    pass


@dataclass
class _Job:
    events: list
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Exception | None = None

    def settle(self, *, result=None, error=None):
        if not self.done.is_set():
            self.result, self.error = result, error
            self.done.set()


class MediaOwner:
    """An unbounded queue, atomically admitted against close/failure.

    Every accepted invocation settles before context exit. There are deliberately
    no invocation deadlines, retries or cancellation on client disconnect.
    """
    record_preview_mode = False

    def __init__(self, path, wake):
        self._path, self._wake = path, wake
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self.done = threading.Event()
        self._open = False
        self._closing = False
        self._destinations = ()
        self.failure = None
        self.cleanup_failure = None
        self._thread = threading.Thread(target=self._run, name='fulcra-media-owner', daemon=True)

    @property
    def record_destination_ids(self):
        return dict(self._destinations)

    def start(self):
        # Thread.start and close share the admission boundary: close cannot
        # return between deciding to start and actually starting the owner.
        with self._lock:
            if self._closing:
                raise OwnerClosed('standalone media owner is closed')
            try:
                self._thread.start()
            except BaseException:
                self._closing = True
                self._open = False
                self._queue.put_nowait(None)
                raise
        self._ready.wait()
        if self.failure is not None:
            self.close()
            raise OwnerFailed('standalone media startup failed') from self.failure
        with self._lock:
            closing = self._closing
        if closing:
            self.close()
            raise OwnerClosed('standalone media startup was closed')

    def ingest_batch(self, events, state):
        # Snapshot objects AND nested enrichment before the admission boundary.
        job = _Job(deepcopy(list(events)))
        with self._lock:
            if not self._open:
                error = OwnerFailed if self.failure is not None else OwnerClosed
                raise error('standalone media owner is closed')
            self._queue.put_nowait(job)
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    def stop_admission(self):
        with self._lock:
            if not self._closing:
                self._closing = True
                self._open = False
                self._queue.put_nowait(None)

    def close(self):
        self.stop_admission()
        if self._thread.ident is not None:
            self._thread.join()

    def _fail(self, exc, current=None):
        with self._lock:
            if self.failure is None:
                self.failure = exc
            self._closing = True
            self._open = False
            error = OwnerFailed('standalone media owner failed')
            error.__cause__ = self.failure
            if current is not None:
                current.settle(error=error)
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    job.settle(error=error)
        self._ready.set()
        self._wake.set()

    def _run(self):
        context = None
        entered = False
        current = None
        exit_info = (None, None, None)
        try:
            with self._lock:
                if self._closing:
                    return
            from .media_config import configured_media
            context = configured_media(self._path)
            run = context.__enter__()
            entered = True
            destinations = run.writer.destination_ids
            from fulcra_common.v1 import _destination
            _destination(destinations.get('watched'), 'Event')
            self._destinations = tuple(destinations.items())
            with self._lock:
                self._open = not self._closing
            self._ready.set()
            while True:
                current = self._queue.get()
                if current is None:
                    break
                try:
                    result = run.import_events(current.events)
                except Exception as exc:
                    current.settle(error=exc)
                else:
                    current.settle(result=result)
                current = None
        except BaseException as exc:
            exit_info = sys.exc_info()
            self._fail(exc, current)
        finally:
            if entered:
                try:
                    context.__exit__(*exit_info)
                except BaseException as exc:
                    self.cleanup_failure = exc
                    self._fail(exc, current)
            try:
                # The runtime uses thread-local cached SQLite connections. This
                # private thread owns them; release them explicitly on that same
                # thread rather than relying on garbage collection after exit.
                from fulcra_collect import db
                db.close_all()
            except BaseException as exc:
                self.cleanup_failure = self.cleanup_failure or exc
                self._fail(exc, current)
            # This event proves context exit, rather than just an empty queue.
            self.done.set()
            self._ready.set()
            self._wake.set()


class ServeLoop:
    """Safe shutdown even when start/serve_forever fails before real loop entry."""
    def __init__(self, server, wake):
        self.server, self.wake = server, wake
        self.entered = threading.Event()
        self.done = threading.Event()
        self.failure = None
        self._gate = threading.Lock()
        self._stop = False
        self._start_attempted = False
        self._start_done = threading.Event()
        original = server.service_actions

        def service_actions():
            # Documented hook runs inside serve_forever's try/finally. Once set,
            # stdlib shutdown is safe even if the loop concurrently raises.
            self.entered.set()
            original()
        server.service_actions = service_actions
        self.thread = threading.Thread(target=self._serve, name='fulcra-webhook', daemon=True)

    def _serve(self):
        try:
            with self._gate:
                if self._stop:
                    return
            self.server.serve_forever(poll_interval=0.1)
        except BaseException as exc:
            self.failure = exc
        finally:
            self.done.set()
            self.wake.set()

    def start(self):
        with self._gate:
            if self._stop:
                raise RuntimeError('webhook serve loop is closed')
            self._start_attempted = True
        try:
            self.thread.start()
        finally:
            # A start exception does not prove no launch. Publish completion
            # independently of its outcome so close can inspect/join the actual
            # thread after every attempted start, including an exceptional one.
            self._start_done.set()

    def close(self):
        with self._gate:
            self._stop = True
            attempted = self._start_attempted
        if attempted:
            self._start_done.wait()
            if self.thread.ident is None:
                return  # Thread.start failed before launching; nothing to join.
            # A pre-call flag cannot prove loop entry. Wait for the documented
            # in-loop hook OR completion; neither path accesses private events.
            while not self.entered.wait(0.01):
                if self.done.is_set():
                    break
            if self.entered.is_set() and not self.done.is_set():
                self.server.shutdown()
            self.thread.join()
