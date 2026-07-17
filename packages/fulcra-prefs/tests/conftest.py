"""FakeFulcraAPI mirrors the exact fulcra_api.core.FulcraAPI methods the
store uses: list_files / resolve_filepath / download_file / upload_file /
fulcra_api (generic request). Keep method signatures in lockstep with the
real library (fulcra-api git file-commands branch, v0.1.30).

VERIFIED against fulcra_api/core.py (v0.1.36):

- resolve_filepath(filepath, all_versions=False): returns a list[dict] of
  matching file records when found (CHANGED in 0.1.36 from a single dict).
  Raises Exception("File not found in Fulcra: <filepath>") when the file is
  absent. Callers take matches[0]["id"] (read_json tolerates a lone dict too).

- list_files(path="/"): returns a dict {"files": [...], ...},
  NOT a plain list. store.py extracts result["files"].

- download_file(file_id): returns http.client.HTTPResponse with .read() method.
  FakeResponse mirrors the .read() interface.

- upload_file(data, file_type, file_size, filepath): signature matches.

- fulcra_api(url_path, method="GET", query=None, data=None,
             return_raw_response=False): real positional order is
  (url_path, method, query, data, ...) — plan fake had query/data before method.
  Corrected here. store.py calls with keyword args so no runtime breakage either
  way, but the fake should faithfully mirror the real lib for documentation
  accuracy. Ingest goes to the TYPED surface POST /ingest/v1/record/{data_type}
  with an unwrapped body {note, recorded_at, sources}; the fake accepts that
  path (and the legacy bare path) and records both body + endpoint.
"""
import io
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent))


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeFulcraAPI:
    def __init__(self):
        self.files: dict[str, bytes] = {}      # path -> content (normalized to absolute)
        self.ingested: list[dict] = []         # posted record bodies (typed unwrapped)
        self.ingest_paths: list[str] = []      # the endpoint each body was POSTed to
        self.fail_ingest = False
        self.fail_upload = False
        self.fail_read = False
        self.validation_errors = None   # set to a message string to simulate a schema error
        self.fail_validate = False      # set True to simulate a catalog/schema-fetch outage

    @staticmethod
    def _abs(path: str) -> str:
        """Normalize paths to absolute (leading slash) to match real API contract."""
        return path if path.startswith("/") else "/" + path

    # --- file library (matches fulcra_api.core.FulcraAPI shapes) ---

    def resolve_filepath(self, filepath, all_versions=False):
        """Returns a list[dict] of matching file records when found (0.1.36
        shape). Raises Exception("File not found in Fulcra: <filepath>") when
        absent — matching the real library's exact error message and shape.
        Callers take matches[0]["id"]."""
        filepath = self._abs(filepath)
        if filepath not in self.files:
            raise Exception(f"File not found in Fulcra: {filepath}")
        return [{"id": f"v-{filepath}", "name": filepath.rsplit('/', 1)[-1]}]

    def download_file(self, file_id):
        path = file_id[2:]                      # "v-<path>"
        return FakeResponse(self.files[path])

    def upload_file(self, data: io.BufferedReader, file_type, file_size, filepath):
        if self.fail_upload:
            raise OSError("simulated file upload outage")
        filepath = self._abs(filepath)
        self.files[filepath] = data.read()
        return {"url": "fake://uploaded", "id": f"v-{filepath}"}

    def delete_file(self, file_id):
        # file_id is "v-<abspath>" (see resolve_filepath / upload_file).
        path = file_id[2:]
        self.files.pop(path, None)

    def list_files(self, path="/"):
        """Returns {"files": [...]} dict mirroring the real library's shape.
        The real list_files wraps results in a top-level dict; callers must
        extract result["files"]."""
        path = self._abs(path)
        prefix = path.rstrip("/") + "/"
        files = [
            {"id": f"v-{p}", "path": p, "name": p.rsplit("/", 1)[-1]}
            for p in sorted(self.files)
            if p.startswith(prefix)
        ]
        return {"files": files}

    # --- generic API request (matches FulcraAPI.fulcra_api) ---
    # Real signature: fulcra_api(url_path, method="GET", query=None, data=None,
    #                            return_raw_response=False)
    def fulcra_api(self, path, method="GET", query=None, data=None,
                   return_raw_response=False):
        # Typed ingest surface: POST /ingest/v1/record/{data_type} with the
        # unwrapped body {note, recorded_at, sources}. (The bare
        # /ingest/v1/record legacy path is still accepted for back-compat.)
        if method == "POST" and (path == "/ingest/v1/record"
                                 or path.startswith("/ingest/v1/record/")):
            if self.fail_ingest:
                raise ConnectionError("simulated ingest outage")
            self.ingested.append(data)
            self.ingest_paths.append(path)
            return b'{"upload_id": "00000000-0000-0000-0000-000000000000"}'
        raise NotImplementedError(path)

    # --- schema pre-flight (mirrors FulcraAPI.validate_records 0.1.37) ---
    # Real signature: validate_records(data_type, records, api_version="v1alpha1")
    # -> list of (record_index, error_message, ValidationError); [] if all valid.
    def validate_records(self, data_type, records, api_version="v1alpha1"):
        if self.fail_validate:
            raise ConnectionError("simulated catalog/schema-fetch outage")
        if self.validation_errors:
            return [(0, self.validation_errors, None)]
        return []

    # --- record reads (mirrors FulcraAPI.moment_annotations) ---
    # Real signature: moment_annotations(start_time, end_time, source=None,
    # fulcra_userid=None) -> list of record dicts. We synthesize the read side
    # from posted ingest bodies so a capture round-trips (ingest -> read) the
    # way the live API does: each posted DataRecordV1 comes back with a server
    # record id, its recorded_at, its sources, and the JSON payload in `data`.
    def moment_annotations(self, start_time=None, end_time=None, source=None,
                           fulcra_userid=None):
        if self.fail_read:
            raise ConnectionError("simulated get-records outage")
        out = []
        for i, body in enumerate(self.ingested):
            # Typed bodies carry the payload in `note` + `sources`/`recorded_at`
            # at top level. (Fall back to the legacy wrapped shape so a
            # hand-posted DataRecordV1 still reads back.)
            if "metadata" in body:                       # legacy envelope
                md = body["metadata"]
                recorded_at, sources = md["recorded_at"], md["source"]
                payload = body["data"]
            else:                                        # typed body
                recorded_at = body.get("recorded_at")
                sources = body.get("sources") or []
                payload = body.get("note")
            out.append({"id": f"rec-{i:04d}",
                        "recorded_at": recorded_at,
                        "sources": sources,
                        "note": payload})               # read side: data-or-note
        return out


@pytest.fixture
def fake_api():
    return FakeFulcraAPI()
