import json, io, os, urllib.error, pytest
from coord_engine import transport as T

class FakeResp:
    def __init__(self, body): self.body=body.encode() if isinstance(body,str) else body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self,*a): return False

def _tr(monkeypatch, opens, token="tok"):
    tr = T.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    monkeypatch.setattr(tr, "_access_token", lambda: token)
    calls=[]
    def fake_open(req, timeout=None):
        calls.append(req.full_url); r=opens.pop(0)
        if isinstance(r, Exception): raise r
        return FakeResp(r)
    monkeypatch.setattr(T.urllib.request, "urlopen", fake_open)
    return tr, calls

def test_http_read_ok_two_gets_and_no_subprocess(monkeypatch):
    tr, calls = _tr(monkeypatch, [json.dumps({"files":[{"id":"v1"}]}), "hello"])
    monkeypatch.setattr(tr, "_run", lambda *a, **k: pytest.fail("CLI must not run on the happy path"))
    assert tr.read_classified("team/x/a.md") == ("hello", "ok")
    assert len(calls)==2 and "name=a.md" in calls[0] and calls[1].endswith("/v1/download")

def test_empty_resolve_is_affirmative_absent(monkeypatch):
    tr,_ = _tr(monkeypatch, [json.dumps({"files":[]})])
    monkeypatch.setattr(tr, "_run", lambda *a, **k: pytest.fail("absent is affirmative; must not fall back"))
    assert tr.read_classified("team/x/gone.md") == (None, "absent")

def test_http_error_falls_back_to_cli_and_never_claims_absent(monkeypatch):
    tr,_ = _tr(monkeypatch, [urllib.error.URLError("boom")])
    import subprocess
    monkeypatch.setattr(tr, "_run", lambda *a, **k: subprocess.CompletedProcess([],0,"from-cli",""))
    assert tr.read_classified("team/x/a.md") == ("from-cli", "ok")

def test_no_token_falls_back(monkeypatch):
    tr,_ = _tr(monkeypatch, [], token=None)
    import subprocess
    monkeypatch.setattr(tr, "_run", lambda *a, **k: subprocess.CompletedProcess([],0,"cli",""))
    assert tr.read_classified("team/x/a.md") == ("cli","ok")

def test_flag_off_uses_cli(monkeypatch):
    monkeypatch.setenv("COORD_TRANSPORT_HTTP","0")
    tr,_ = _tr(monkeypatch, [])
    import subprocess
    monkeypatch.setattr(tr, "_run", lambda *a, **k: subprocess.CompletedProcess([],0,"cli",""))
    assert tr.read_classified("team/x/a.md") == ("cli","ok")

def test_token_memoized_success_only(monkeypatch):
    tr = T.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    monkeypatch.delenv("FULCRA_ACCESS_TOKEN", raising=False)
    n={"c":0}
    def fake_run(argv, timeout, **kw):
        n["c"]+=1
        return (0,"tok-%d"%n["c"],"") if n["c"]>1 else (1,"","fail")
    monkeypatch.setattr(T, "run_bounded", fake_run)
    assert tr._access_token() is None and n["c"]==1      # failure NOT cached
    assert tr._access_token() == "tok-2" and n["c"]==2
    assert tr._access_token() == "tok-2" and n["c"]==2   # success memoized
