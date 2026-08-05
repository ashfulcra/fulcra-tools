"""Production adapter for ``coord-engine acceptance pair``."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import time
import uuid
from typing import Any

from . import acceptance_pair, okf, records, tasks


def _json_line(raw: str) -> dict[str, Any] | None:
    """Return the first complete JSON object line; stderr may follow stdout."""
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class _AcceptancePairAdapter:
    def __init__(self, args: argparse.Namespace, transport: Any) -> None:
        from . import cli

        self.cli = cli
        self.args = args
        self.transport = transport
        self.team = args.team
        self.agent = args.agent
        self.peer = args.peer
        self.timeout = args.timeout
        self.nonce = args.nonce or uuid.uuid4().hex
        title = f"acceptance pair nonce {self.nonce}"
        summary = f"Pairwise acceptance nonce: {self.nonce}"
        next_action = f"respond with nonce {self.nonce}"
        self.title = title
        self.summary = summary
        self.next_action = next_action
        payload = cli._directive_payload(title, summary, next_action, self.peer)
        self.slug = f"{tasks.slugify(title)}-{cli._payload_hash(payload)}"
        nonce_key = hashlib.sha256(self.nonce.encode("utf-8")).hexdigest()[:12]
        self.role = f"acceptance-peer-{tasks.agent_key(self.peer)}-{nonce_key}"
        self.role_path = self.cli._role_doc_path(self.team, self.role)
        self.response_path: str | None = None
        self.checkpoint_ref: str | None = None

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.cli.main(argv, self.transport)
        return rc, out.getvalue() + err.getvalue()

    def _poll_queue(self, identity: str, kind: str) -> acceptance_pair.HopResult:
        deadline = time.monotonic() + self.timeout
        last = ""
        while True:
            rc, raw = self._run([
                "queue", self.team, "--agent", identity, "--peek",
                "--no-obligations", "--json",
            ])
            last = raw
            if rc != 0:
                return acceptance_pair.HopResult(
                    False, f"queue rc={rc} while awaiting {kind}", raw)
            envelope = _json_line(raw)
            if envelope is None:
                return acceptance_pair.HopResult(False, "queue returned malformed JSON", raw)
            for event in envelope.get("events", []):
                if event.get("kind") != kind or event.get("slug") != self.slug:
                    continue
                ptr = event.get("ptr")
                if not isinstance(ptr, str) or not ptr:
                    return acceptance_pair.HopResult(False, f"{kind} event has no pointer", raw)
                path = ptr if ptr.startswith("team/") else f"team/{self.team}/{ptr}"
                body = self.transport.read(path)
                if body is None:
                    return acceptance_pair.HopResult(False, f"{kind} pointer unreadable: {path}", raw)
                if self.nonce not in body:
                    return acceptance_pair.HopResult(
                        False, f"BAD NONCE in {kind} pointer {path}", body)
                if kind == "response":
                    self.response_path = path
                return acceptance_pair.HopResult(True, f"matched {kind} {self.slug}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return acceptance_pair.HopResult(
                    False, f"timed out after {self.timeout:g}s awaiting {kind}", last)
            time.sleep(min(2.0, remaining))

    def prove_delivery(self, identity: str) -> acceptance_pair.HopResult:
        rc, raw = self._run([
            "doctor", self.team, "--delivery", "--agent", identity,
            "--deadline", str(self.timeout),
        ])
        return acceptance_pair.HopResult(
            rc == 0 and "delivery: PROVEN" in raw,
            "delivery PROVEN" if rc == 0 and "delivery: PROVEN" in raw
            else f"doctor delivery rc={rc}",
            raw,
        )

    def tell(self) -> acceptance_pair.HopResult:
        rc, raw = self._run([
            "tell", self.team, self.peer, self.title,
            "--priority", "P0", "--workstream", "acceptance",
            "--summary", self.summary, "--next", self.next_action,
            "--from", self.agent,
        ])
        body = self.transport.read(self.cli._task_path(self.team, self.slug))
        ok = rc == 0 and body is not None and self.nonce in body
        if ok:
            cfg = records.load_config(self.transport, self.team)
            ok = cfg is not None and records.emit_event(
                self.transport, cfg, sender=self.agent, to=self.peer,
                kind="directive", priority="P0", slug=self.slug,
                ptr=f"task/{self.slug}.md", team=self.team,
            )
        task_path = self.cli._task_path(self.team, self.slug)
        evidence = body or raw
        return acceptance_pair.HopResult(
            ok,
            f"directive {self.slug} verified at {task_path}" if ok
            else f"tell rc={rc}, read-back, or event emission failed",
            evidence,
        )

    def peer_reads_directive(self) -> acceptance_pair.HopResult:
        return self._poll_queue(self.peer, "directive")

    def peer_responds(self) -> acceptance_pair.HopResult:
        evidence = f"pairwise acceptance nonce {self.nonce}"
        rc, raw = self._run([
            "respond", self.team, self.slug, "--agent", self.peer,
            "--outcome", self.nonce, "--evidence", evidence,
        ])
        if rc != 0:
            return acceptance_pair.HopResult(False, f"respond rc={rc}", raw)
        prefix = f"{self.cli._responses_prefix(self.team)}{self.slug}/"
        try:
            names = sorted(
                (e.get("name") or "") for e in self.transport.list_dir(prefix)
                if not e.get("is_dir") and (e.get("name") or "").endswith(".md"))
        except Exception as exc:
            return acceptance_pair.HopResult(False, f"response listing failed: {exc}", raw)
        for name in reversed(names):
            path = prefix + name
            body = self.transport.read(path)
            if body and self.nonce in body:
                self.response_path = path
                cfg = records.load_config(self.transport, self.team)
                if cfg is None or not records.emit_event(
                    self.transport, cfg, sender=self.peer, to=self.agent,
                    kind="response", priority="P0", slug=self.slug, ptr=path,
                    team=self.team,
                ):
                    return acceptance_pair.HopResult(False, "response event emission failed", raw)
                return acceptance_pair.HopResult(True, f"response {path} and event verified")
        return acceptance_pair.HopResult(False, "response shard missing or BAD NONCE", raw)

    def agent_reads_response(self) -> acceptance_pair.HopResult:
        return self._poll_queue(self.agent, "response")

    def peer_parks(self) -> acceptance_pair.HopResult:
        if self.transport.read(self.role_path) is None:
            role_doc = okf.render_frontmatter({
                "type": "Role", "policy": "shared", "sla_hours": 24,
                "maintainer": self.agent,
            }) + "\nEphemeral role used by pairwise acceptance.\n"
            if not self.transport.write(self.role_path, role_doc):
                return acceptance_pair.HopResult(False, "acceptance role write failed")
        rc1, raw1 = self._run([
            "roles", "claim", self.team, self.role, "--agent", self.peer,
            "--summary", f"pair acceptance {self.nonce}",
        ])
        objective = f"pairwise acceptance nonce {self.nonce}"
        rc2, raw2 = self._run([
            "continuity", "park", self.team, "--agent", self.peer,
            "--role", self.role,
            "--objective", objective, "--next", "GET-ON-THE-BUS final join",
        ])
        raw = raw1 + raw2
        role_doc = okf.parse_frontmatter(self.transport.read(self.role_path)) or {}
        ref = role_doc.get("checkpoint_ref")
        self.checkpoint_ref = str(ref) if ref else None
        snap_raw = self.transport.read(str(ref)) if ref else None
        try:
            snap = json.loads(snap_raw) if snap_raw else None
        except ValueError:
            snap = None
        ok = (rc1 == 0 and rc2 == 0 and isinstance(snap, dict)
              and self.nonce in str(snap.get("objective") or ""))
        return acceptance_pair.HopResult(
            ok, f"checkpoint {ref} verified" if ok else "CHECKPOINT NOT WRITTEN or nonce mismatch", raw)

    def agent_resumes_peer(self) -> acceptance_pair.HopResult:
        task = f"role-{tasks.slugify(self.role)}"
        rc, raw = self._run([
            "continuity", "resume", self.team, self.peer, task,
            "--json", "--max-age", "5m",
        ])
        data = _json_line(raw)
        if data is None:
            return acceptance_pair.HopResult(False, "resume returned malformed JSON", raw)
        age = data.get("checkpoint_age_seconds")
        ok = (rc == 0 and self.nonce in str(data.get("objective") or "")
              and isinstance(age, (int, float)) and age < 300)
        return acceptance_pair.HopResult(
            ok, f"nonce matched; checkpoint age {age:.1f}s" if ok
            else "resume nonce/freshness verification failed", raw)

    def final_join(self) -> acceptance_pair.HopResult:
        raw_parts: list[str] = []
        for identity in (self.agent, self.peer):
            rc, raw = self._run([
                "presence", "beat", self.team, "--agent", identity,
                "--summary", f"pair acceptance joined {self.nonce}",
            ])
            raw_parts.append(raw)
            path = f"{self.cli._presence_prefix(self.team)}{tasks.agent_key(identity)}.md"
            body = self.transport.read(path)
            if rc != 0 or body is None or self.nonce not in body:
                return acceptance_pair.HopResult(
                    False, f"presence join failed for {identity}", "".join(raw_parts))
        rc, raw = self._run([
            "roles", "release", self.team, self.role, "--agent", self.peer,
        ])
        raw_parts.append(raw)
        if rc != 0:
            return acceptance_pair.HopResult(
                False, "acceptance lease cleanup failed", "".join(raw_parts))
        residue = [self.role_path]
        if self.checkpoint_ref:
            residue.append(self.checkpoint_ref)
        for path in residue:
            if not self.transport.delete_idempotent(path):
                return acceptance_pair.HopResult(
                    False, f"acceptance cleanup failed for {path}", "".join(raw_parts))
        return acceptance_pair.HopResult(
            True, "both identities joined; nonce role/checkpoint/lease cleaned up")


def cmd_acceptance_pair(args: argparse.Namespace, transport: Any) -> int:
    adapter = _AcceptancePairAdapter(args, transport)
    return acceptance_pair.run_pair(
        adapter, agent=args.agent, peer=args.peer,
    )
