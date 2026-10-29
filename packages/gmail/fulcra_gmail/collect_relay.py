"""Canonical coord operations bound to Collect, without shell/CLI credentials.

The engine still owns directive slugs, readback, companions and presence. Only
its transport changes. A cancellation latch survives the engine's intentionally
best-effort companion/presence exception handlers and invalidates the result.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import PurePosixPath
import threading

from fulcra_api.core import FulcraAPI
from fulcra_api.credentials import FulcraCredentials
from fulcra_common.json_values import snapshot_json
from fulcra_collect.files_io import CollectFiles, FilesIOError, MissingFile

# stdout/stderr capture is process-global. Collect runs Gmail in a dedicated
# worker process; concurrent/nested invocations here fail closed.
_ENGINE_LOCK = threading.Lock()


class _CollectSDK(FulcraAPI):
    """Reuse SDK catalog/schema/record transformations over guarded HTTP only."""
    def __init__(self, files):
        super().__init__(credentials=FulcraCredentials(access_token=files.session._token))
        self.files = files

    def fulcra_api(self, url_path, method="GET", query=None, data=None,
                   return_http_response=False, content_type="application/json", authenticated=True):
        if return_http_response or not authenticated or not url_path.startswith("/") or url_path.startswith("//"):
            raise ValueError("Unsupported Collect coord SDK request")
        kwargs = {"params": query} if query else {}
        if data is not None:
            value = snapshot_json(data)
            if content_type == "application/x-jsonl":
                values = value if isinstance(value, list) else [value]
                body = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n" for row in values)
            else:
                body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            kwargs.update(content=body.encode("utf-8"), headers={"Content-Type": content_type})
        status, _, body = self.files._request(method, url_path, **kwargs)
        if status >= 300:
            raise FilesIOError("Collect coord API response is unknown")
        return body


class CollectCoordTransport:
    timeout = 30
    public_read_v2_enabled = False
    conditional_writes_supported = False

    def __init__(self, session, team, *, files=None):
        if (not isinstance(team, str) or not team or team in {".", ".."}
                or "/" in team or "\\" in team or any(ord(c) < 33 for c in team)):
            raise ValueError("Invalid Collect coord team")
        self.session, self.team = session, team
        self.cancelled = False
        self.files = files if files is not None else CollectFiles(session, guard=self.guard)
        self._sdk = None

    def guard(self, **kwargs):
        if self.cancelled:
            raise RuntimeError("Collect relay lifecycle was cancelled")
        try:
            self.session.guard(**kwargs)
        except Exception:
            self.cancelled = True
            raise

    def _path(self, path):
        if (not isinstance(path, str) or path.startswith("//") or "\\" in path
                or any(ord(c) < 32 for c in path)):
            raise ValueError("Invalid Collect coord path")
        parsed = PurePosixPath("/" + path.lstrip("/"))
        if ".." in parsed.parts or not str(parsed).startswith(f"/team/{self.team}/"):
            raise ValueError("Collect coord path is outside the captured team")
        return str(parsed)

    def _effect(self, call, *, mutating=False):
        self.guard(mutating=mutating)
        try:
            result = call()
        except (FilesIOError, ValueError, OSError):
            self.guard(mutating=mutating)
            raise
        except RuntimeError:
            # The API request hook can reject a changed principal before a
            # response exists. Never let an optional engine handler swallow it.
            self.cancelled = True
            raise
        self.guard(mutating=mutating)
        return result

    def read_classified(self, path, *, deadline=None):
        if deadline is not None and deadline.expired():
            return None, "error"
        try:
            remote = self._path(path)
            timeout = min(self.timeout, deadline.remaining()) if deadline is not None and deadline.remaining() is not None else self.timeout
            body = self._effect(lambda: self.files.read_text(remote, timeout=timeout))
            if deadline is not None and deadline.expired():
                return None, "error"
            return body, "ok"
        except MissingFile:
            return None, "absent"
        except Exception:
            return None, "error"

    def read(self, path):
        return self.read_classified(path)[0]

    def list_dir(self, prefix):
        from coord_engine.transport import TransportError
        try:
            remote = self._path(prefix)
            return self._effect(lambda: self.files.list_dir(remote, timeout=self.timeout))
        except Exception:
            raise TransportError("Collect coord listing is unknown") from None

    def write(self, path, content):
        try:
            remote = self._path(path)
            self._effect(lambda: self.files.write_text(remote, content, timeout=self.timeout), mutating=True)
            return True
        except Exception:
            return False

    def delete(self, path):
        try:
            remote = self._path(path)
            self._effect(lambda: self.files.delete(remote, timeout=self.timeout), mutating=True)
            return True
        except Exception:
            return False

    def record_write(self, data_type, api_version, note, source, recorded_at=None, tags=None):
        from fulcra_api.cli.record import record
        try:
            self.guard(mutating=True)
            if self._sdk is None:
                self._sdk = _CollectSDK(self.files)
            # Invoke the standalone SDK command. The root CLI callback loads
            # another credential store even when passed obj=; never enter it.
            args = [data_type, "--api-version", api_version, "--source", source,
                    "--note=" + json.dumps(note)]
            if recorded_at:
                args.append("--recorded_at=" + json.dumps(recorded_at))
            if tags:
                args.append("--tags=" + json.dumps(list(tags)))
            self._effect(lambda: record.main(args, obj=self._sdk, standalone_mode=False), mutating=True)
            return True
        except Exception:
            return False

    def verify_directive(self, slug, directive):
        from coord_engine import model, okf
        if not isinstance(slug, str) or not slug or "/" in slug or "\\" in slug:
            return False
        body, state = self.read_classified(f"team/{self.team}/task/{slug}.md")
        if state != "ok":
            return False
        row = okf.parse_frontmatter(body)
        valid = (model.is_task(row) and row.get("id") == slug
                 and "kind:directive" in row.get("tags", [])
                 and row.get("title") == directive.title
                 and row.get("description") == directive.summary
                 and row.get("assignee") == directive.assignee
                 and row.get("next_action") == directive.next_action)
        self.guard()
        return bool(valid)

    def run(self, argv):
        from coord_engine import bus_tags, cli
        if (not isinstance(argv, list) or len(argv) < 2 or argv[0] not in {"tell", "search"}
                or argv[1] != self.team or not _ENGINE_LOCK.acquire(blocking=False)):
            return 1, ""
        try:
            self.guard(mutating=argv[0] == "tell")
            bus_tags.cache_clear()
            cli._ACTIVITY_BEAT_MEMO.clear()
            stdout, stderr = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = cli.main(argv, transport=self)
                self.guard(mutating=argv[0] == "tell")
            except (Exception, SystemExit):
                return 1, ""
            return (rc, stdout.getvalue()) if not self.cancelled else (1, "")
        except Exception:
            return 1, ""
        finally:
            _ENGINE_LOCK.release()

    def close(self):
        self.files.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
