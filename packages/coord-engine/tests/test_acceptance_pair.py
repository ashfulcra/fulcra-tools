from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_engine import acceptance_pair, cli, commands_acceptance, records
from coord_engine_test_helpers import FakeTransport


class FakeAdapter:
    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def _hop(self, name: str) -> acceptance_pair.HopResult:
        self.calls.append(name)
        if self.fail == name:
            detail = {
                "read-directive": "BAD NONCE: expected n-good, got n-wrong",
                "park": "CHECKPOINT NOT WRITTEN: checkpoint_ref absent",
            }.get(name, f"{name} failed")
            return acceptance_pair.HopResult(False, detail, f"raw {detail}")
        return acceptance_pair.HopResult(True, f"{name} okay")

    def prove_delivery(self, identity: str) -> acceptance_pair.HopResult:
        return self._hop(f"doctor:{identity}")

    def tell(self) -> acceptance_pair.HopResult:
        return self._hop("tell")

    def peer_reads_directive(self) -> acceptance_pair.HopResult:
        return self._hop("read-directive")

    def peer_responds(self) -> acceptance_pair.HopResult:
        return self._hop("respond")

    def agent_reads_response(self) -> acceptance_pair.HopResult:
        return self._hop("read-response")

    def peer_parks(self) -> acceptance_pair.HopResult:
        return self._hop("park")

    def agent_resumes_peer(self) -> acceptance_pair.HopResult:
        return self._hop("resume")

    def final_join(self) -> acceptance_pair.HopResult:
        return self._hop("join")


def test_pair_all_hops_emit_positive_heartbeat() -> None:
    adapter = FakeAdapter()
    lines: list[str] = []
    ticks = iter(float(n) for n in range(20))

    rc = acceptance_pair.run_pair(
        adapter, agent="a", peer="b", emit=lines.append, clock=lambda: next(ticks))

    assert rc == 0
    assert [line.split()[1:3] for line in lines[:-1]] == [
        [str(n), "PASS"] for n in range(1, 10)]
    assert lines[-1].startswith("PASS pair a<->b")
    assert adapter.calls == [
        "doctor:a", "doctor:b", "tell", "read-directive", "respond",
        "read-response", "park", "resume", "join",
    ]


def test_bad_nonce_fails_loud_at_directive_read_and_stops() -> None:
    adapter = FakeAdapter(fail="read-directive")
    lines: list[str] = []

    rc = acceptance_pair.run_pair(adapter, agent="a", peer="b", emit=lines.append)

    assert rc == 1
    assert lines[-2].startswith("FAILED AT HOP 4")
    assert "BAD NONCE" in lines[-2]
    assert "raw BAD NONCE" in lines[-1]
    assert adapter.calls == ["doctor:a", "doctor:b", "tell", "read-directive"]


def test_missing_checkpoint_fails_loud_and_never_resumes() -> None:
    adapter = FakeAdapter(fail="park")
    lines: list[str] = []

    rc = acceptance_pair.run_pair(adapter, agent="a", peer="b", emit=lines.append)

    assert rc == 1
    assert lines[-2].startswith("FAILED AT HOP 7")
    assert "CHECKPOINT NOT WRITTEN" in "\n".join(lines[-2:])
    assert adapter.calls[-1] == "park"
    assert "resume" not in adapter.calls


def test_cli_dispatches_acceptance_pair(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class Adapter:
        def __init__(self, args, transport) -> None:
            seen.update(team=args.team, agent=args.agent, peer=args.peer,
                        timeout=args.timeout, nonce=args.nonce, transport=transport)

    monkeypatch.setattr(commands_acceptance, "_AcceptancePairAdapter", Adapter)
    monkeypatch.setattr(
        acceptance_pair, "run_pair",
        lambda adapter, *, agent, peer: seen.update(run=(agent, peer)) or 0,
    )
    transport = object()

    rc = cli.main([
        "acceptance", "pair", "r", "--agent", "a", "--peer", "b",
        "--timeout", "7", "--nonce", "fixed",
    ], transport)

    assert rc == 0
    assert seen == {
        "team": "r", "agent": "a", "peer": "b", "timeout": 7.0,
        "nonce": "fixed", "transport": transport, "run": ("a", "b"),
    }


class PairTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.window: list[dict[str, object]] = []

    def record_write(self, data_type, api_version, note, source, recorded_at=None):
        self.window.append({
            "id": f"pair-r{len(self.window)}",
            "recorded_at": recorded_at or "2026-08-03T18:00:00Z",
            "note": note,
            "sources": [source],
        })
        return True

    def records(self, data_type, since, until):
        return list(self.window)


@pytest.mark.parametrize("loaded", [False, True], ids=["fresh-store", "loaded-store"])
def test_production_pair_adapter_passes_fresh_and_loaded_store(
        monkeypatch, tmp_path, capsys, loaded: bool) -> None:
    now = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "_now", lambda: now)
    monkeypatch.setenv("COORD_ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = PairTransport()
    t.put(records.config_path("r"), json.dumps({
        "data_type": "MomentAnnotation/x", "api_version": "v1alpha1",
    }))
    if loaded:
        real_ref = "team/r/member/b/continuity/role-real/latest.json"
        t.put("team/r/roles/real.md",
              f"---\ntype: Role\nsla_hours: 24\ncheckpoint_ref: {real_ref}\n---\n")
        t.put(real_ref, json.dumps({"objective": "real work must survive"}))
        assert cli.main(["roles", "claim", "r", "real", "--agent", "b"], t) == 0
        capsys.readouterr()
        t.put("team/r/task/unrelated.md",
              "---\ntype: Task\ntitle: Unrelated\nstatus: active\nassignee: somebody\n---\n")
        t.window.append({
            "id": "old-unrelated", "recorded_at": "2026-08-03T17:59:00Z",
            "sources": ["somebody"],
            "note": records.build_payload(
                to="somebody-else", kind="directive", priority="P3", slug="old"),
        })

    rc = cli.main([
        "acceptance", "pair", "r", "--agent", "a", "--peer", "b",
        "--timeout", "0.1", "--nonce", f"fixed-{loaded}",
    ], t)

    output = capsys.readouterr().out
    assert rc == 0, output
    assert "HOP 9 PASS" in output
    assert "PASS pair a<->b" in output
    if loaded:
        real_role = cli.okf.parse_frontmatter(t.store["team/r/roles/real.md"])
        assert real_role["checkpoint_ref"] == real_ref
        assert json.loads(t.store[real_ref])["objective"] == "real work must survive"
