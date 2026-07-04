import json
import httpx


def run_main(ni, argv, handler, monkeypatch, capsys):
    monkeypatch.setattr(ni, "get_token", lambda: "test-token")
    monkeypatch.setattr(
        ni, "make_api_client",
        lambda token: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.example.test",
        ),
    )
    code = ni.main(argv)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return code, json.loads(out)


def _happy_handler(req):
    if req.url.path == "/user/v1alpha1/annotation" and req.method == "GET":
        return httpx.Response(200, json=[{
            "id": "def-1", "name": "Watched",
            "description": "com.fulcradynamics.annotation.media.watched",
            "annotation_type": "duration"}])
    if req.url.path == "/ingest/v1/record/batch":
        return httpx.Response(200)
    return httpx.Response(404)


def test_main_happy_path_envelope(ni, fixtures_dir, monkeypatch, capsys):
    code, env = run_main(
        ni, [str(fixtures_dir / "slim.csv"), "--json", "--no-verify"],
        _happy_handler, monkeypatch, capsys)
    assert code == 0
    assert env["ok"] is True
    assert env["importer"] == "netflix"
    assert env["variant"] == "slim"
    assert env["total"] == 5 and env["posted"] == 5
    assert env["errors"] == []
    assert set(env) >= {"importer", "ok", "total", "posted",
                        "skipped_existing", "verified", "would_post", "errors"}


def test_main_check_only(ni, fixtures_dir, monkeypatch, capsys):
    code, env = run_main(
        ni, [str(fixtures_dir / "gdpr.csv"), "--json", "--check-only"],
        _happy_handler, monkeypatch, capsys)
    assert code == 0
    assert env["would_post"] == 3 and env["posted"] == 0


def test_main_parse_error_envelope(ni, tmp_path, monkeypatch, capsys):
    p = tmp_path / "junk.csv"; p.write_text("a,b\n1,2\n")
    code, env = run_main(ni, [str(p), "--json"], _happy_handler, monkeypatch, capsys)
    assert code == 2
    assert env["ok"] is False
    assert env["errors"][0]["stage"] == "parse"


def test_main_auth_failure_envelope(ni, fixtures_dir, monkeypatch, capsys):
    def handler(req):
        return httpx.Response(401, json={"detail": "bad token"})
    code, env = run_main(ni, [str(fixtures_dir / "slim.csv"), "--json"],
                         handler, monkeypatch, capsys)
    assert code == 2
    assert env["errors"][0]["stage"] == "auth"
