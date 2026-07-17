"""B3 relay emitter — deterministic outbox directive + readback + restart dedupe.

The directive's identity fields (title/summary/next/assignee) are byte-stable
functions of the ledger ``outbox_key``, so re-emitting the SAME relay converges
on ONE visible coord directive. No PII ever enters a directive (opaque
outbox_key + rule id only).
"""
from __future__ import annotations

import pytest

from fulcra_gmail import ledger as ledger_mod
from fulcra_gmail.relay import (
    DEFAULT_COORD_BINARY,
    CoordEngineRelayEmitter,
    RelayDirective,
    build_directive,
    resolve_coord_binary,
)
from fulcra_gmail.rules import parse_rules

#: The PATH launchd actually hands the collect daemon — no ~/.local/bin.
_LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _rule(**over):
    raw = {
        "id": "receipts",
        "version": 3,
        "name": "Receipts",
        "match": "subject:receipt",
        "actions": ["file", "relay"],
        "relay_to": "agent:claude",
        "relay_priority": "P1",
    }
    raw.update(over)
    return parse_rules([raw])[0]


def _outbox():
    return ledger_mod.outbox_key("acct-1", "m1", "receipts", 3)


def test_resolves_from_install_dir_under_the_daemons_minimal_path(tmp_path, monkeypatch):
    """The regression: under launchd's PATH the bare name is unresolvable.

    coord-engine installs to ~/.local/bin, which is NOT on the PATH launchd
    gives the daemon — so exec'ing the bare name raised FileNotFoundError, every
    relay poll failed, and the all-polls-failed guard took the whole gmail
    plugin down (68 consecutive failures, nothing filed). Resolution must fall
    back to the real install dir rather than trusting PATH.
    """
    fake_bin = tmp_path / ".local" / "bin"
    fake_bin.mkdir(parents=True)
    shim = fake_bin / DEFAULT_COORD_BINARY
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)

    monkeypatch.setenv("PATH", _LAUNCHD_PATH)          # what the daemon gets
    monkeypatch.delenv("FULCRA_COORD_ENGINE_BIN", raising=False)
    monkeypatch.setattr("fulcra_gmail.relay._FALLBACK_BIN_DIRS", (str(fake_bin),))

    assert resolve_coord_binary() == str(shim)


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_ENGINE_BIN", "/custom/coord-engine")
    assert resolve_coord_binary() == "/custom/coord-engine"


def test_absolute_binary_passes_through(monkeypatch):
    monkeypatch.delenv("FULCRA_COORD_ENGINE_BIN", raising=False)
    assert resolve_coord_binary("/opt/x/coord-engine") == "/opt/x/coord-engine"


def test_missing_binary_raises_actionable_error(tmp_path, monkeypatch):
    """A genuinely absent coord-engine must say what to do — not a bare ENOENT."""
    monkeypatch.setenv("PATH", _LAUNCHD_PATH)
    monkeypatch.delenv("FULCRA_COORD_ENGINE_BIN", raising=False)
    monkeypatch.setattr("fulcra_gmail.relay._FALLBACK_BIN_DIRS", (str(tmp_path / "nope"),))
    with pytest.raises(FileNotFoundError) as exc:
        resolve_coord_binary()
    msg = str(exc.value)
    assert "FULCRA_COORD_ENGINE_BIN" in msg
    assert "launchd" in msg


def test_directive_is_byte_stable_function_of_outbox_key():
    rule = _rule()
    key = _outbox()
    d1 = build_directive(key, rule)
    d2 = build_directive(key, rule)
    assert d1 == d2
    # assignee + priority come from the rule; the key is embedded in identity.
    assert d1.assignee == "agent:claude"
    assert d1.priority == "P1"
    assert key in d1.title


def test_directive_carries_no_pii():
    # The directive text is built from ONLY the opaque outbox_key + rule id —
    # no message content is ever passed to build_directive, so no subject/from/
    # body can appear. Assert the opaque tokens are present and that nothing
    # resembling an email address leaks.
    rule = _rule()
    key = _outbox()
    d = build_directive(key, rule)
    blob = " ".join([d.title, d.summary, d.next_action])
    # build_directive is never handed message content, so nothing can leak.
    assert ".com" not in blob and "example" not in blob
    assert key in blob  # opaque outbox key present
    assert "receipts@3" in blob  # opaque rule identity is fine


def test_different_outbox_keys_give_different_directives():
    rule = _rule()
    k1 = ledger_mod.outbox_key("acct-1", "m1", "receipts", 3)
    k2 = ledger_mod.outbox_key("acct-1", "m2", "receipts", 3)
    assert build_directive(k1, rule) != build_directive(k2, rule)


class FakeCoordStore:
    """Records ``tell`` invocations, deduping by the coord identity payload
    (title, summary, next, assignee) the real engine keys on."""

    def __init__(self) -> None:
        self.docs: dict[tuple, dict] = {}
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        verb = argv[0]
        if verb == "tell":
            # tell <team> <assignee> <title> -s .. -n .. -p .. --from ..
            team, assignee, title = argv[1], argv[2], argv[3]
            opts = _parse_opts(argv[4:])
            payload = (title, opts.get("-s", ""), opts.get("-n", ""), assignee)
            slug = f"{_slug(title)}-{abs(hash(payload)) % (10 ** 8):08d}"
            if payload in self.docs:
                return 0, f"directive {self.docs[payload]['slug']} already delivered\n"
            self.docs[payload] = {"slug": slug, "title": title, "assignee": assignee,
                                  "summary": opts.get("-s", ""), "team": team}
            return 0, f"directive {slug} -> {assignee}\n"
        if verb == "search":
            # search <team> <query> --json
            query = argv[2]
            hits = [d for d in self.docs.values()
                    if query in d["title"] or query in d["summary"]]
            import json as _json
            return 0, _json.dumps(hits)
        return 1, ""


def _parse_opts(rest: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    i = 0
    while i < len(rest):
        if rest[i].startswith("-") and i + 1 < len(rest):
            opts[rest[i]] = rest[i + 1]
            i += 2
        else:
            i += 1
    return opts


def _slug(title: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "task"


def test_emit_delivers_and_readback_confirms():
    store = FakeCoordStore()
    emitter = CoordEngineRelayEmitter("team-x", run=store.run)
    d = build_directive(_outbox(), _rule())
    result = emitter.emit(d)
    assert result.ok
    assert emitter.exists(d)


def test_restart_reemit_reconciles_to_one_directive():
    store = FakeCoordStore()
    emitter = CoordEngineRelayEmitter("team-x", run=store.run)
    d = build_directive(_outbox(), _rule())
    emitter.emit(d)
    # A crash-then-restart re-emits the byte-identical directive.
    result2 = emitter.emit(d)
    assert result2.ok
    # Exactly one visible directive despite two emits.
    tells = [c for c in store.calls if c[0] == "tell"]
    assert len(tells) == 2
    assert len(store.docs) == 1


def test_exists_false_before_emit():
    store = FakeCoordStore()
    emitter = CoordEngineRelayEmitter("team-x", run=store.run)
    d = build_directive(_outbox(), _rule())
    assert not emitter.exists(d)


def test_emit_reports_failure_on_nonzero_rc():
    def failing_run(argv):
        return 1, "boom"

    emitter = CoordEngineRelayEmitter("team-x", run=failing_run)
    d = build_directive(_outbox(), _rule())
    result = emitter.emit(d)
    assert not result.ok


def test_directive_dataclass_shape():
    d = RelayDirective(
        outbox_key="relay-abc", title="t", summary="s", next_action="n",
        assignee="agent:x", priority="P2",
    )
    assert d.assignee == "agent:x"
