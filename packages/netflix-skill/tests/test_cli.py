"""Pure local CLI and pre-runtime capability/option failures."""
import builtins
import json
from pathlib import Path

import pytest


def test_check_only_without_any_runtime_or_httpx(ni, fixtures_dir, monkeypatch, capsys):
    from fulcra_netflix import loader

    original = builtins.__import__
    def denied(name, *args, **kwargs):
        if name.startswith(("httpx", "fulcra_common", "fulcra_collect", "fulcra_media", "fulcra_netflix.native")):
            raise ImportError("SECRET credential-canary")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", denied)
    module = loader.load()
    assert module.main([str(fixtures_dir / "gdpr.csv"), "--check-only", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["would_post"] == 3 and env["posted"] == 0 and env["status"] == "checked"


@pytest.mark.parametrize("args", [["--no-verify"], ["--no-verify", "--check-only"],
    ["--data-type", "Metric/11111111-2222-4333-8444-555555555555"],
    ["--data-type", "Event/not-a-uuid"]])
def test_invalid_options_before_auth_or_source(ni, tmp_path, monkeypatch, capsys, args):
    from fulcra_collect import standalone_v1
    def forbidden(*args, **kwargs):
        pytest.fail("runtime/source entered")
    monkeypatch.setattr(standalone_v1, "standalone_runtime", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert ni.main([str(tmp_path / "missing"), "--json", *args]) == 2
    env = json.loads(capsys.readouterr().out)
    assert env["errors"][0]["stage"] == "args"


@pytest.mark.parametrize("fault", ["adapter-old", "adapter-bool", "shared-old", "shared-bool",
    "namespace", "callable", "builder", "progress", "adapter-missing"])
def test_missing_or_incompatible_runtime_before_auth_source(ni, tmp_path, monkeypatch, capsys, fault):
    from fulcra_collect import standalone_v1
    from fulcra_collect.v1_outbox import V1RecordOutbox
    from fulcra_common import v1
    from fulcra_netflix import native
    def forbidden(*args, **kwargs):
        pytest.fail("runtime/source entered")
    monkeypatch.setattr(standalone_v1, "standalone_runtime", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    if fault.startswith("adapter-") and fault != "adapter-missing":
        monkeypatch.setattr(native, "CONTRACT_VERSION", True if fault.endswith("bool") else 0)
    elif fault.startswith("shared-"):
        monkeypatch.setattr(standalone_v1, "STANDALONE_RUNTIME_VERSION", True if fault.endswith("bool") else 0)
    elif fault == "namespace":
        monkeypatch.setattr(standalone_v1, "standalone_namespace_supported", lambda _: False)
    elif fault == "callable":
        monkeypatch.setattr(V1RecordOutbox, "pending_destinations", None)
    elif fault == "builder":
        monkeypatch.setattr(v1, "build_event_record", None)
    elif fault == "progress":
        monkeypatch.setattr(V1RecordOutbox, "progress", None)
    else:
        original = builtins.__import__
        def denied(name, *args, **kwargs):
            if name == "fulcra_netflix":
                raise ImportError("SECRET credential-canary")
            return original(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", denied)
    assert ni.main([str(tmp_path / "empty.csv"), "--json"]) == 2
    output = capsys.readouterr()
    env = json.loads(output.out)
    assert env["errors"][0]["stage"] == "runtime" and "SECRET" not in output.out + output.err


@pytest.mark.parametrize("source", ["a,b\n1,2\n", "Title,Date\nSECRET,SECRET\n"])
def test_pure_parse_error_is_sanitized(ni, tmp_path, capsys, source):
    path = tmp_path / "synthetic.csv"
    path.write_text(source)
    assert ni.main([str(path), "--json", "--check-only"]) == 2
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
    assert json.loads(output.out)["errors"][0]["stage"] == "parse"


@pytest.mark.parametrize("args", [["--SECRET"], ["--data-type"]])
def test_syntactic_option_errors_have_one_sanitized_json(ni, tmp_path, capsys, args):
    assert ni.main([str(tmp_path / "missing"), "--json", *args]) == 2
    output = capsys.readouterr()
    env = json.loads(output.out)
    assert not env["ok"] and env["errors"][0]["stage"] == "args"
    assert "SECRET" not in output.out + output.err
