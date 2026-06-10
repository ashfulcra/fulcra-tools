"""FakeFulcraAPI mirrors the exact fulcra_api.core.FulcraAPI methods the
store uses: list_files / resolve_filepath / download_file / upload_file /
fulcra_api (generic request). Keep method signatures in lockstep with the
real library (fulcra-api>=0.1.33).

VERIFIED against /tmp/fulcra-api-python/fulcra_api/core.py (v0.1.30):

- resolve_filepath(filepath, all_versions=False): raises Exception when the
  file is not found (does NOT return []). store.py wraps it in try/except.

- list_files(path="/", state="uploaded"): returns a dict {"files": [...], ...},
  NOT a plain list. store.py extracts result["files"].

- download_file(file_id): returns http.client.HTTPResponse with .read() method.
  FakeResponse mirrors the .read() interface.

- upload_file(data, file_type, file_size, filepath): signature matches.

- fulcra_api(url_path, method="GET", query=None, data=None,
             return_http_response=False): real positional order is
  (url_path, method, query, data, ...) — plan fake had query/data before method.
  Corrected here. store.py calls with keyword args so no runtime breakage either
  way, but the fake should faithfully mirror the real lib for documentation
  accuracy.
"""
import io
import json
import pytest


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeFulcraAPI:
    def __init__(self):
        self.files: dict[str, bytes] = {}      # path -> content
        self.ingested: list[dict] = []         # posted record bodies
        self.fail_ingest = False

    # --- file library (matches fulcra_api.core.FulcraAPI shapes) ---

    def resolve_filepath(self, filepath, all_versions=False):
        """Returns list of file dicts if found. Raises Exception if not found,
        mirroring the real library's behaviour (it never returns [])."""
        if filepath not in self.files:
            raise Exception(f"File not found in Fulcra: {filepath}")
        return [{"id": f"v-{filepath}", "name": filepath.rsplit('/', 1)[-1]}]

    def download_file(self, file_id):
        path = file_id[2:]                      # "v-<path>"
        return FakeResponse(self.files[path])

    def upload_file(self, data: io.BufferedReader, file_type, file_size, filepath):
        self.files[filepath] = data.read()
        return {"url": "fake://uploaded", "id": f"v-{filepath}"}

    def list_files(self, path="/", state="uploaded"):
        """Returns {"files": [...]} dict mirroring the real library's shape.
        The real list_files wraps results in a top-level dict; callers must
        extract result["files"]."""
        prefix = path.rstrip("/") + "/"
        files = [
            {"id": f"v-{p}", "path": p, "name": p.rsplit("/", 1)[-1]}
            for p in sorted(self.files)
            if p.startswith(prefix)
        ]
        return {"files": files}

    # --- generic API request (matches FulcraAPI.fulcra_api) ---
    # Real signature: fulcra_api(url_path, method="GET", query=None, data=None,
    #                            return_http_response=False)
    def fulcra_api(self, path, method="GET", query=None, data=None,
                   return_http_response=False):
        if path == "/ingest/v1/record" and method == "POST":
            if self.fail_ingest:
                raise ConnectionError("simulated ingest outage")
            self.ingested.append(data)
            return b"{}"
        raise NotImplementedError(path)


@pytest.fixture
def fake_api():
    return FakeFulcraAPI()
