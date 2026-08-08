import json, io, os, urllib.error, pytest
from coord_engine import transport as T  # noqa: F811

class FakeResp:
    def __init__(self, body): self.body=body.encode() if isinstance(body,str) else body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self,*a): return False

def _tr(monkeypatch, opens, token="tok"):
    tr = T.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    # accepts **_ because the real signature takes the remaining budget:
    # every phase of an op spends ONE deadline, so the token fetch is bounded
    # by what is left rather than by a fresh copy of the per-op timeout.
    monkeypatch.setattr(tr, "_access_token", lambda **_: token)
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


# --- one deadline for the whole op ------------------------------------------
#
# The blocking finding on round 1: read_classified had FOUR independent
# `self.timeout` bounds stacked in series — token fetch, resolve, download, and
# the CLI fallback — so a configured 30s per-op bound could take 120s. Every
# fold budget in the engine assumes the per-op bound actually holds, so this is
# not a latency nicety; it is the property the budgets are built on.
#
# These assert the BOUND HANDED TO EACH PHASE, not merely that a deadline
# object exists. A version that opened a deadline and then passed self.timeout
# anyway would satisfy any weaker check while keeping the exact defect.

def test_every_http_phase_receives_the_REMAINING_budget_not_a_fresh_one(monkeypatch):
    import urllib.request
    from coord_engine import transport as T  # noqa: F811
    tr = T.FulcraFileTransport(command=["fake"], timeout=30.0)
    monkeypatch.setattr(tr, "_http_enabled", lambda: True)
    seen: list = []
    monkeypatch.setattr(tr, "_access_token", lambda **kw: seen.append(("token", kw.get("budget"))) or "tok")

    class _R:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    bodies = [b'{"files":[{"id":"v1"}]}', b'hello']
    def _open(req, timeout=None):
        seen.append(("http", timeout))
        return _R(bodies[len([s for s in seen if s[0] == "http"]) - 1])
    monkeypatch.setattr(urllib.request, "urlopen", _open)

    assert tr.read_classified("team/x/a.md") == ("hello", "ok")
    budgets = [b for _, b in seen]
    assert len(budgets) == 3, f"expected token+resolve+download, got {seen}"
    assert all(b is not None and b <= 30.0 for b in budgets), budgets
    # strictly non-increasing: each phase sees what the previous one left
    assert budgets == sorted(budgets, reverse=True), budgets


def test_the_cli_fallback_is_bound_by_what_the_http_legs_left(monkeypatch):
    from coord_engine import transport as T  # noqa: F811
    tr = T.FulcraFileTransport(command=["fake"], timeout=30.0)
    monkeypatch.setattr(tr, "_http_enabled", lambda: True)
    monkeypatch.setattr(tr, "_http_read", lambda path, deadline=None: (None, "error"))
    got: dict = {}
    def _run(args, timeout=None, **kw):
        got["timeout"] = timeout
        import subprocess
        return subprocess.CompletedProcess(args, 0, "from-cli", "")
    monkeypatch.setattr(tr, "_run", _run)
    assert tr.read_classified("team/x/a.md") == ("from-cli", "ok")
    assert got["timeout"] is not None, "fallback got the un-shared default"
    assert got["timeout"] <= 30.0


def test_an_exhausted_budget_returns_error_and_NEVER_absent(monkeypatch):
    """The fail-closed edge. If the HTTP legs burn the whole budget the CLI
    fallback cannot run inside the caller's bound — and the answer must be
    UNREADABLE, never a missing file, or a slow network invents an absence."""
    from coord_engine import transport as T  # noqa: F811
    tr = T.FulcraFileTransport(command=["fake"], timeout=30.0)
    monkeypatch.setattr(tr, "_http_enabled", lambda: True)

    def _burn(path, deadline=None):
        deadline.instant = 0.0          # spend it
        return None, "error"
    monkeypatch.setattr(tr, "_http_read", _burn)
    monkeypatch.setattr(tr, "_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("fallback ran with no budget left")))
    assert tr.read_classified("team/x/a.md") == (None, "error")
