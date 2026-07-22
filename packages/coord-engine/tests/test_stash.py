"""Tests for the stash verb (BUS-75) — durable per-agent tooling stash.

`stash push/pull/list` is the deterministic bookkeeping for the
fulcra-agent-durable-state pattern: a thin layer over the transport with a
manifest + sha256 checksums and a FAIL-CLOSED secrets guard. The guard is most
of the point — a token in `team/<team>/**` is readable by every agent on the
bus, so a false refusal costs one `--unsafe-allow-secrets`, while a false
allow costs a rotation and an incident.

Cheap-beats-clever: stdlib-only, FakeTransport in-memory store, no network.
"""

import argparse
import hashlib
import json

import pytest

from coord_engine import cli, stash
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
AGENT = "worker"
PREFIX = f"team/{TEAM}/_coord/agents/{AGENT}/stash/"


def _args(**kw):
    ns = argparse.Namespace(team=TEAM, agent=AGENT, json=False,
                            unsafe_allow_secrets=False, dest=None, names=[])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _push(tmp_path, transport, name, content, **kw):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return cli.cmd_stash_push(_args(files=[str(p)], **kw), transport)


def _manifest(transport):
    return json.loads(transport.store[PREFIX + "manifest.json"])


# --- secrets guard (pure) ---------------------------------------------------

@pytest.mark.parametrize("name", [
    ".env", "prod.env", ".env.local", "deploy.key", "server.pem",
    "api-token.txt", "TOKEN_CACHE", "secrets.yaml", "credentials.json",
    "id_rsa", "id_ed25519",
])
def test_guard_refuses_secret_shaped_names(name):
    assert stash.secret_reason(name, "harmless") is not None


@pytest.mark.parametrize("content", [
    "export LINEAR=lin_oauth_abc123DEF",
    "key = sk-abcdefghijklmnop",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_guard_refuses_credential_shaped_content(content):
    assert stash.secret_reason("loop.sh", content) is not None


def test_guard_allows_ordinary_tooling():
    # "task-1" contains the letters "sk-" — a naive substring guard would
    # refuse every file that mentions a task id. The guard must be token-shaped.
    ok = "#!/bin/bash\necho task-1 done  # see whiskers.md\n"
    assert stash.secret_reason("listener-loop.sh", ok) is None


# --- push -------------------------------------------------------------------

def test_push_uploads_file_and_writes_manifest(tmp_path):
    t = FakeTransport()
    rc = _push(tmp_path, t, "loop.sh", "#!/bin/bash\necho hi\n")
    assert rc == 0
    assert t.store[PREFIX + "loop.sh"] == "#!/bin/bash\necho hi\n"
    m = _manifest(t)
    assert m["schema"] == stash.MANIFEST_SCHEMA
    entry = m["files"]["loop.sh"]
    body = "#!/bin/bash\necho hi\n"
    assert entry["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert entry["size"] == len(body.encode())


def test_push_records_executable_bit(tmp_path):
    t = FakeTransport()
    p = tmp_path / "loop.sh"
    p.write_text("#!/bin/bash\n", encoding="utf-8")
    p.chmod(0o755)
    assert cli.cmd_stash_push(_args(files=[str(p)]), t) == 0
    assert _manifest(t)["files"]["loop.sh"]["exec"] is True


def test_push_merges_into_existing_manifest(tmp_path):
    t = FakeTransport()
    _push(tmp_path, t, "a.sh", "a\n")
    _push(tmp_path, t, "b.sh", "b\n")
    assert set(_manifest(t)["files"]) == {"a.sh", "b.sh"}


def test_push_refuses_secret_file_fail_closed(tmp_path, capsys):
    t = FakeTransport()
    rc = _push(tmp_path, t, "deploy.key", "harmless-looking\n")
    assert rc == 1
    assert PREFIX + "deploy.key" not in t.store
    assert PREFIX + "manifest.json" not in t.store  # nothing half-pushed
    assert "refused" in capsys.readouterr().err


def test_push_refuses_whole_batch_when_one_file_is_secret(tmp_path):
    # Fail closed means the BATCH fails: no partial upload that a retry
    # without the offending file would silently diverge from.
    t = FakeTransport()
    ok = tmp_path / "loop.sh"; ok.write_text("fine\n", encoding="utf-8")
    bad = tmp_path / "prod.env"; bad.write_text("X=1\n", encoding="utf-8")
    rc = cli.cmd_stash_push(_args(files=[str(ok), str(bad)]), t)
    assert rc == 1
    assert t.store == {}


def test_push_override_flag_lets_a_flagged_file_through(tmp_path, capsys):
    t = FakeTransport()
    rc = _push(tmp_path, t, "api-token.txt", "not actually a token\n",
               unsafe_allow_secrets=True)
    assert rc == 0
    assert PREFIX + "api-token.txt" in t.store
    assert "WARNING" in capsys.readouterr().err


def test_push_refuses_binary_files(tmp_path, capsys):
    t = FakeTransport()
    p = tmp_path / "tool.bin"
    p.write_bytes(b"\x00\x01\xff\xfe")
    rc = cli.cmd_stash_push(_args(files=[str(p)]), t)
    assert rc == 1
    assert t.store == {}
    assert "binary" in capsys.readouterr().err.lower()


def test_push_missing_local_file_errors(tmp_path):
    t = FakeTransport()
    rc = cli.cmd_stash_push(_args(files=[str(tmp_path / "absent.sh")]), t)
    assert rc == 1
    assert t.store == {}


# --- list -------------------------------------------------------------------

def test_list_shows_entries_with_manifest_state(tmp_path, capsys):
    t = FakeTransport()
    _push(tmp_path, t, "loop.sh", "x\n")
    t.put(PREFIX + "orphan.sh", "y\n")  # in the store, not in the manifest
    assert cli.cmd_stash_list(_args(), t) == 0
    out = capsys.readouterr().out
    assert "loop.sh" in out and "orphan.sh" in out
    assert "unmanifested" in out


def test_list_empty_stash(capsys):
    t = FakeTransport()
    assert cli.cmd_stash_list(_args(), t) == 0
    assert "empty" in capsys.readouterr().out


def test_list_json_is_pure(tmp_path, capsys):
    t = FakeTransport()
    _push(tmp_path, t, "loop.sh", "x\n")
    capsys.readouterr()  # drain the push chatter; the gate is list's output alone
    assert cli.cmd_stash_list(_args(json=True), t) == 0
    rows = json.loads(capsys.readouterr().out)  # json.loads = purity gate
    assert rows[0]["name"] == "loop.sh"
    assert rows[0]["manifest"] == "ok"


# --- pull -------------------------------------------------------------------

def test_pull_restores_files_and_verifies_checksums(tmp_path):
    t = FakeTransport()
    _push(tmp_path, t, "loop.sh", "#!/bin/bash\n")
    dest = tmp_path / "restore"
    rc = cli.cmd_stash_pull(_args(dest=str(dest)), t)
    assert rc == 0
    assert (dest / "loop.sh").read_text(encoding="utf-8") == "#!/bin/bash\n"


def test_pull_restores_executable_bit(tmp_path):
    t = FakeTransport()
    src = tmp_path / "loop.sh"
    src.write_text("#!/bin/bash\n", encoding="utf-8")
    src.chmod(0o755)
    assert cli.cmd_stash_push(_args(files=[str(src)]), t) == 0
    dest = tmp_path / "restore"
    assert cli.cmd_stash_pull(_args(dest=str(dest)), t) == 0
    assert (dest / "loop.sh").stat().st_mode & 0o100


def test_pull_clears_stale_executable_bit(tmp_path):
    # Manifest says exec=False: restoring over a pre-existing EXECUTABLE dest
    # must clear the bit, not preserve it — "re-apply the manifest exec bit"
    # goes both directions (codex finding, PR #450 r1).
    t = FakeTransport()
    _push(tmp_path, t, "loop.sh", "plain\n")
    dest = tmp_path / "restore"
    dest.mkdir()
    stale = dest / "loop.sh"
    stale.write_text("old\n", encoding="utf-8")
    stale.chmod(0o755)
    assert cli.cmd_stash_pull(_args(dest=str(dest)), t) == 0
    assert not (stale.stat().st_mode & 0o111)


def test_parse_manifest_drops_malformed_entries():
    raw = json.dumps({"schema": stash.MANIFEST_SCHEMA,
                      "files": {"ok.sh": {"sha256": "x"}, "bad.sh": "bad-entry"}})
    files = stash.parse_manifest(raw)["files"]
    assert "ok.sh" in files and "bad.sh" not in files


def test_list_survives_malformed_manifest_entry(capsys):
    # A structurally corrupt entry is remote data — it degrades to
    # "unmanifested", it must never traceback list (codex finding, PR #450 r1).
    t = FakeTransport()
    t.put(PREFIX + "tool.sh", "x\n")
    t.put(PREFIX + "manifest.json",
          json.dumps({"schema": stash.MANIFEST_SCHEMA,
                      "files": {"tool.sh": "bad-entry"}}))
    assert cli.cmd_stash_list(_args(), t) == 0
    assert "unmanifested" in capsys.readouterr().out


def test_pull_survives_malformed_manifest_entry(tmp_path):
    t = FakeTransport()
    t.put(PREFIX + "tool.sh", "x\n")
    t.put(PREFIX + "manifest.json",
          json.dumps({"schema": stash.MANIFEST_SCHEMA,
                      "files": {"tool.sh": "bad-entry"}}))
    dest = tmp_path / "restore"
    assert cli.cmd_stash_pull(_args(dest=str(dest)), t) == 0
    assert (dest / "tool.sh").read_text(encoding="utf-8") == "x\n"


def test_pull_selected_names_only(tmp_path):
    t = FakeTransport()
    _push(tmp_path, t, "a.sh", "a\n")
    _push(tmp_path, t, "b.sh", "b\n")
    dest = tmp_path / "restore"
    assert cli.cmd_stash_pull(_args(dest=str(dest), names=["a.sh"]), t) == 0
    assert (dest / "a.sh").exists() and not (dest / "b.sh").exists()


def test_pull_reports_checksum_drift(tmp_path, capsys):
    t = FakeTransport()
    _push(tmp_path, t, "loop.sh", "original\n")
    t.put(PREFIX + "loop.sh", "tampered\n")  # store changed under the manifest
    dest = tmp_path / "restore"
    rc = cli.cmd_stash_pull(_args(dest=str(dest)), t)
    assert rc == 1
    assert "drift" in capsys.readouterr().err
    # the bytes still land (an operator can inspect), but the exit is loud
    assert (dest / "loop.sh").exists()


def test_pull_refuses_path_traversal_names(tmp_path, capsys):
    t = FakeTransport()
    dest = tmp_path / "restore"
    rc = cli.cmd_stash_pull(_args(dest=str(dest), names=["../evil.sh"]), t)
    assert rc == 1
    assert not (tmp_path / "evil.sh").exists()


def test_pull_empty_stash_says_so(tmp_path, capsys):
    t = FakeTransport()
    rc = cli.cmd_stash_pull(_args(dest=str(tmp_path / "restore")), t)
    assert rc == 1
    assert "empty" in (capsys.readouterr().err + capsys.readouterr().out)


def test_pull_unknown_name_errors(tmp_path):
    t = FakeTransport()
    _push(tmp_path, t, "a.sh", "a\n")
    rc = cli.cmd_stash_pull(_args(dest=str(tmp_path / "r"), names=["nope.sh"]), t)
    assert rc == 1
