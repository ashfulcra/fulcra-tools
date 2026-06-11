"""FakeFulcraAPI mirrors the exact fulcra_api.core.FulcraAPI methods the
store uses: list_files / resolve_filepath / download_file / upload_file /
fulcra_api (generic request). Keep method signatures in lockstep with the
real library (fulcra-api git file-commands branch, v0.1.30).

VERIFIED against fulcra_api/core.py (v0.1.30, file-commands branch):

- resolve_filepath(filepath): returns ONE dict (the file record) when found.
  Raises Exception("File not found in Fulcra Library: <filepath>") when the
  file is absent. Does NOT return a list — callers do match["id"], not
  matches[0]["id"].

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
  accuracy.
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
        self.ingested: list[dict] = []         # posted record bodies
        self.fail_ingest = False
        self.fail_upload = False

    @staticmethod
    def _abs(path: str) -> str:
        """Normalize paths to absolute (leading slash) to match real API contract."""
        return path if path.startswith("/") else "/" + path

    # --- file library (matches fulcra_api.core.FulcraAPI shapes) ---

    def resolve_filepath(self, filepath, all_versions=False):
        """Returns ONE dict (the file record) when found.
        Raises Exception("File not found in Fulcra Library: <filepath>") when
        absent — matching the real library's exact error message and shape.
        Callers do match["id"], NOT matches[0]["id"]."""
        filepath = self._abs(filepath)
        if filepath not in self.files:
            raise Exception(f"File not found in Fulcra Library: {filepath}")
        return {"id": f"v-{filepath}", "name": filepath.rsplit('/', 1)[-1]}

    def download_file(self, file_id):
        path = file_id[2:]                      # "v-<path>"
        return FakeResponse(self.files[path])

    def upload_file(self, data: io.BufferedReader, file_type, file_size, filepath):
        if self.fail_upload:
            raise OSError("simulated file upload outage")
        filepath = self._abs(filepath)
        self.files[filepath] = data.read()
        return {"url": "fake://uploaded", "id": f"v-{filepath}"}

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
        if path == "/ingest/v1/record" and method == "POST":
            if self.fail_ingest:
                raise ConnectionError("simulated ingest outage")
            self.ingested.append(data)
            return b"{}"
        raise NotImplementedError(path)


@pytest.fixture
def fake_api():
    return FakeFulcraAPI()
