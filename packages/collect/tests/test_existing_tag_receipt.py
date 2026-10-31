"""Remember proven existing tags without granting creation authority."""

import subprocess
import urllib.request

import httpx
import keyring
import pytest

from fulcra_collect import config, macos_keychain
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.data_types import CreationJournal
from test_v1_session import ACCOUNT, OTHER_ACCOUNT, context, token

TAG = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
OTHER_TAG = "bbbbbbbb-bbbb-4ccc-8ddd-123456789abc"


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    from fulcra_api.credentials_store import CredentialsFile

    def fail(*args, **kwargs):
        pytest.fail("external credential, process or network access")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)
    monkeypatch.setattr(urllib.request, "urlopen", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    for method in ("get_password", "set_password", "delete_password", "get_keyring"):
        monkeypatch.setattr(keyring, method, fail)
    for method in ("get_password", "set_password", "delete_password"):
        if hasattr(macos_keychain, method):
            monkeypatch.setattr(macos_keychain, method, fail)
    monkeypatch.setattr(CredentialsFile, "load", fail)
    monkeypatch.setattr(CredentialsFile, "save", fail)


def test_remember_existing_is_a_read_only_lookup_then_compatible_receipt(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path, monkeypatch)
    paths = []
    renamed = [False]

    def handle(request):
        paths.append((request.method, request.url.path))
        assert request.method == "GET"
        if renamed[0]:
            assert request.url.path.endswith("/tag/id/" + TAG)
        else:
            assert request.url.path.endswith("/tag/name/synthetic")
        return httpx.Response(
            200,
            json={
                "id": TAG,
                "name": "renamed" if renamed[0] else "synthetic",
                "fulcra_userid": ACCOUNT,
            },
        )

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handle)) as session:
        assert session.remember_existing_tag("synthetic", TAG) == TAG
    journal = CreationJournal(
        config.config_dir() / "v1-type-creations.sqlite", read_only=True
    )
    assert journal.read_tag(ACCOUNT, "synthetic") == ("resolved", TAG)
    renamed[0] = True
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handle)) as session:
        assert session.resolve_tag("synthetic", allow_create=False) == TAG
    assert len(paths) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "replacement",
        "expected-mismatch",
        "noncanonical",
        "account",
        "disabled",
        "during-account",
        "during-disabled",
        "conflicting-receipt",
    ],
)
def test_remember_existing_holds_on_changed_proof_without_post_or_misbinding(
    tmp_path, monkeypatch, fault
):
    ctx = context(tmp_path, monkeypatch)
    auth = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: auth[0])
    calls = []
    journal_path = config.config_dir() / "v1-type-creations.sqlite"

    def revoke():
        if "account" in fault:
            auth[0] = token(OTHER_ACCOUNT)
        else:
            cfg = config.load()
            cfg.disable("synthetic")
            if fault.startswith("during-"):
                # Inject a changed authoritative read; attempting an exclusive
                # config save inside the helper's held shared lease deadlocks.
                monkeypatch.setattr(config, "load", lambda: cfg)
            else:
                config.save(cfg)

    def handle(request):
        calls.append(request.method)
        assert request.method == "GET"
        if fault.startswith("during-"):
            revoke()
        if fault == "conflicting-receipt":
            CreationJournal(journal_path).resolved_tag(ACCOUNT, "synthetic", OTHER_TAG)
        if fault == "missing":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "id": OTHER_TAG if fault == "replacement" else TAG,
                "name": "synthetic",
                "fulcra_userid": ACCOUNT,
            },
        )

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handle)) as session:
        if fault in ("account", "disabled"):
            revoke()
        expected = OTHER_TAG if fault == "expected-mismatch" else TAG
        if fault == "noncanonical":
            expected = TAG.upper()
        with pytest.raises((ValueError, RuntimeError)):
            session.remember_existing_tag("synthetic", expected)
    assert "POST" not in calls
    if fault == "conflicting-receipt":
        assert CreationJournal(journal_path, read_only=True).read_tag(
            ACCOUNT, "synthetic"
        ) == ("resolved", OTHER_TAG)
    else:
        assert not journal_path.exists()
