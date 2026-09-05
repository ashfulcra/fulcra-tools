"""Review verdict tally — the deterministic core of the fulcra-agent-review skill.

Requesting a review and submitting a verdict are single-file writes (prose). Folding
multiple reviewers' verdicts into an overall state is a fold → code. Pure functions
here; the I/O wrapper + CLI live in ``cli.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

APPROVED = "APPROVED"
CHANGES = "CHANGES"
PENDING = "PENDING"

_APPROVE = {"approve", "approved", "lgtm"}
_CHANGES = {"changes", "request-changes", "reject", "rejected"}
_EXACT_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def accepted_vocabulary() -> str:
    """The verdict tokens that count, rendered for an error message.

    Public because a caller that has to reach into ``_APPROVE`` to tell a
    reviewer why their verdict was ignored will eventually drift from it — and
    a stale list in that message is worse than none, since it sends the reviewer
    to re-file with another token that also does not count.
    """
    return (f"{'|'.join(sorted(_APPROVE))} (approve) / "
            f"{'|'.join(sorted(_CHANGES))} (changes)")


def normalize_head(value: Any) -> Optional[str]:
    """Return a canonical exact commit id, or ``None``.

    BUS-86 review rounds are keyed by a full Git object id, never by a moving
    branch name or abbreviated SHA. SHA-1 and SHA-256 object ids are accepted.
    """
    head = str(value or "").strip().lower()
    return head if _EXACT_HEAD.fullmatch(head) else None


#: The APPEND-ONLY verdict suffix: `--<iso>-<digest>` before `.md`.
#:
#: Two forms are first-class, forever (coord-boss ruling b99fb8da):
#:   PLAIN   `<head>--<reviewer>.md`                  — hand-writers, unchanged
#:   APPEND  `<head>--<reviewer>--<iso>-<digest>.md`  — the verb
#:
#: The verb uses the append form because this store has no create-if-absent and
#: no versioned write, so writing a SHARED name is check-then-write and cannot
#: protect evidence: codex-reviewer reproduced a concurrent CHANGES being
#: overwritten by APPROVE with rc 0 (595 r2). A unique name never touches an
#: existing file, which closes verb-vs-verb AND verb-vs-hand races without any
#: store primitive. The plain form keeps working untouched — no migration, and
#: nobody writing shards by hand breaks on ship day.
_APPEND_SUFFIX = re.compile(
    r"--(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)-(?P<digest>[0-9a-f]{6,16})$")


def verdict_filename(reviewer: str, *, head: Optional[str] = None,
                     ts: Optional[str] = None,
                     digest: Optional[str] = None) -> str:
    """Filename for one requirement's verdict in the active review round.

    With ``ts``+``digest`` this is the APPEND-ONLY form — a name no other writer
    can be holding. Without them it is the historical plain form, which stays
    valid for hand-writers.
    """
    if head and ts and digest:
        return f"{head}--{reviewer}--{ts}-{digest}.md"
    return f"{head}--{reviewer}.md" if head else f"{reviewer}.md"


def parse_verdict_filename(
    name: str, *, head: Optional[str] = None
) -> Optional[tuple[str, Optional[str]]]:
    """``(reviewer, ts_or_None)`` for a verdict filename, or ``None``.

    ``ts`` is present only for the append-only form; a plain shard carries its
    time in frontmatter (or, failing that, the listing mtime), because it was
    written before the name had anywhere to put it.
    """
    if not name.endswith(".md"):
        return None
    stem = name[:-3]
    if head:
        prefix = f"{head}--"
        if not stem.startswith(prefix):
            return None
        rest = stem[len(prefix):]
        if not rest:
            return None
        m = _APPEND_SUFFIX.search(rest)
        if m:
            reviewer = rest[:m.start()]
            return (reviewer, m.group("ts")) if reviewer else None
        return (rest, None)
    # Legacy unkeyed review: the historical `<reviewer>.md` layout only.
    return (stem, None) if "--" not in stem else None


def reviewer_from_filename(name: str, *, head: Optional[str] = None) -> Optional[str]:
    """Decode the requirement token from a verdict filename for ``head``.

    Head-keyed reviews ignore every superseded head before reading its shard.
    Legacy unkeyed reviews retain the historical ``<reviewer>.md`` layout.
    """
    parsed = parse_verdict_filename(name, head=head)
    return parsed[0] if parsed else None


_FRACTION = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.(\d{1,6})Z$")


def canonical_sort_key(name_ts: Optional[str], fm_ts: Optional[str],
                       mtime_ts: Optional[str]) -> str:
    """One form for every shard's ordering key: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    WHY (codex-reviewer + codex-coder, coord-fold round 12, 2026-09-05): the
    append-only name carries SECOND precision plus a content digest, and the
    fold broke same-second ties on the name — so two verdicts from one reviewer
    in one second were ordered by DIGEST, not chronology. codex-reviewer
    reproduced an earlier APPROVE (digest feb86aee) outranking a later CHANGES
    (058ddb93): folded APPROVED on stale evidence.

    The SECOND comes from the ACL-controlled filename whenever it has one — a
    frontmatter value can never move a shard across seconds, so a reviewer can
    only reorder their OWN same-second shards, which they could do anyway by
    filing again. The FRACTION comes from the frontmatter ``ts`` only when its
    second equals the name's; the verb writes microseconds there. Shards with no
    fraction get ``.000000`` so legacy and new shards compare in ONE format —
    mixed precision compared as strings is exactly the misordering the verb's
    own comment warns about. Same-microsecond shards from one reviewer are not
    a case the fold can order; the name still breaks that tie, deterministically.
    """
    base = name_ts or fm_ts or mtime_ts or ""
    if len(base) < 19:
        return ""
    second = base[:19]
    fraction = "000000"
    if fm_ts and fm_ts[:19] == second:
        m = _FRACTION.match(fm_ts)
        if m:
            fraction = m.group(1).ljust(6, "0")
    return f"{second}.{fraction}Z"


def content_digest(raw: Optional[str]) -> str:
    """Identity of a shard's CURRENT bytes: sha256, 16 hex. A rewrite in place changes it."""
    import hashlib
    return hashlib.sha256((raw or "").encode()).hexdigest()[:16]


def _resolves(source: dict[str, Any], entry: str, targets: dict[str, dict[str, Any]]) -> tuple[Optional[str], str]:
    """Does ``entry`` (from ``source['supersedes']``) resolve a target? -> (target name or None, why).

    ONLY THE STORE CAN SUPPLY CAUSALITY (both reviewers, review-winning-envelope
    r7). A name is predictable; a client-written nonce is editable frontmatter at
    a mutable path — codex-reviewer rewrote a plain CHANGES in place keeping its
    nonce and the old edge still resolved it; codex-coder predeclared a nonce
    and wrote the later CHANGES to match. So an edge is honoured only when:

      * ``name@sha256:<digest>`` matches the target's CURRENT bytes (a rewrite in
        place un-resolves it), and
      * the STORE's mtime — server-assigned, never client-selectable — proves
        the target strictly EARLIER than the source (a later-written target, or
        an in-place rewrite, is never resolved; the same minute, or an unknown
        mtime, is not proof and fails closed).

    A bare legacy name needs the same mtime proof and binds no digest.
    """
    name, _, token = entry.partition("@")
    if name == source.get("name"):
        return None, "self-link"
    target = targets.get(name)
    if target is None:
        return None, "resolves nothing"
    if token:
        if not token.startswith("sha256:") or token[7:] != (target.get("digest") or ""):
            return None, "digest mismatch"
    t_at, s_at = target.get("mtime_iso") or "", source.get("mtime_iso") or ""
    if t_at and s_at and t_at < s_at:
        return name, "ok"
    return None, "no causal proof"


def fold_newest_per_reviewer(
    rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Keep ONE shard per reviewer; return ``(kept, folded_away)``.

    THE RULE (codex-coder, review-winning-envelope r4/r6): ordering by client
    timestamp can never carry the correction contract, and neither can a name —
    a name is predictable (selectable timestamp, deterministic digest), so an
    older APPROVE could predeclare and erase a later CHANGES. Supersession is
    explicit AND causal:

      * a shard is RESOLVED when another shard of the same reviewer quotes it
        as ``name@sha256:<digest>`` matching its CURRENT bytes AND the store's
        mtime proves it strictly earlier than the quoting shard (see
        ``_resolves``; a bare legacy name needs the same mtime proof);
      * any UNRESOLVED CHANGES dominates, whatever its timestamp;
      * otherwise the newest live shard wins (canonical key, then name).

    Equal keys, unnamed conflicts, dangling names, digest mismatches, self-links
    and unproven causality all FAIL CLOSED to CHANGES. The typed verb quotes the
    content digest of every prior shard it can list AND read; a hand-written
    APPROVE must quote digests itself. Amends ruling b99fb8da (newest-wins); constraint 5 — a
    stale CHANGES must not block forever — holds through the link the verb
    writes. Supersession stays auditable: every shard stays on disk and the
    folded count is returned.
    """
    by: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by.setdefault(row["reviewer"], []).append(row)
    kept: list[dict[str, Any]] = []
    folded = 0
    for reviewer in sorted(by):
        shards = by[reviewer]
        targets = {r.get("name"): r for r in shards}
        resolved = set()
        for r in shards:
            for entry in (r.get("supersedes") or []):
                hit, _why = _resolves(r, str(entry), targets)
                if hit:
                    resolved.add(hit)
        live = [r for r in shards if r.get("name") not in resolved] or shards
        blocking = [r for r in live if normalize_verdict(r.get("verdict")) == "changes"]
        pool = blocking or live
        winner = max(pool, key=lambda r: (r.get("sort_key") or "", r.get("name") or ""))
        kept.append(winner)
        folded += len(shards) - 1
    return kept, folded


def invalid_supersession_edges(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Every edge the fold did NOT honour, with why — so a reader can say the graph was malformed
    rather than silently folding around it: self-links, dangling names, digest mismatches, and any
    entry without the store's mtime proving its target earlier."""
    by: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["reviewer"], {})[r.get("name")] = r
    out: list[dict[str, str]] = []
    for r in rows:
        for entry in (r.get("supersedes") or []):
            hit, why = _resolves(r, str(entry), by[r["reviewer"]])
            if hit is None:
                out.append({"shard": r.get("name") or "", "edge": str(entry), "why": why})
    return out


def normalize_verdict(v: Optional[str]) -> Optional[str]:
    s = (v or "").strip().lower()
    if s in _APPROVE:
        return "approve"
    if s in _CHANGES:
        return "changes"
    return None


def tally(
    verdicts: list[dict[str, Any]], *, required: Optional[list[str]] = None
) -> dict[str, Any]:
    """Fold reviewer verdicts into an overall state.

    - **CHANGES** if any reviewer requests changes (a single blocker dominates).
    - **APPROVED** if there's at least one approval, no outstanding changes, and —
      when ``required`` reviewers are named — all of them have approved.
    - **PENDING** otherwise (no verdicts, or required reviewers haven't voted).
    """
    by_reviewer: dict[str, str] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        nv = normalize_verdict(v.get("verdict"))
        who = str(v.get("reviewer") or "")
        if nv and who:
            by_reviewer[who] = nv  # last verdict per reviewer wins
    approvals = [r for r, d in by_reviewer.items() if d == "approve"]
    changes = [r for r, d in by_reviewer.items() if d == "changes"]
    if changes:
        state = CHANGES
    elif approvals and (not required or all(r in approvals for r in required)):
        state = APPROVED
    else:
        state = PENDING
    return {
        "state": state,
        "approvals": sorted(approvals),
        "changes": sorted(changes),
        "required": required or [],
        "pending_required": sorted(r for r in (required or []) if r not in by_reviewer),
    }


def is_pending_for(pending_required: list, agent: str,
                   role_holders: "dict[str, list[str]] | None" = None) -> bool:
    """True iff agent owes a verdict: it is named directly in
    pending_required, or a name there is a ROLE whose fresh lease holders
    (per role_holders) include the agent. Role-routing doctrine: review
    requests SHOULD name roles, not identities — this matcher honors both."""
    for r in pending_required or []:
        if r == agent:
            return True
        if agent in (role_holders or {}).get(r, ()):
            return True
    return False

def evidence_digest(names: Any) -> str:
    """Fingerprint the verdict shards a cache summarises.

    THE CACHE IS BOUND TO ITS EVIDENCE (codex-reviewer, 595 r4). Deleting a
    mutable cache cannot stop another writer recreating it: a `review status`
    that read the old tally, paused, and resumed AFTER a correction landed
    rewrote `.settled` from its stale snapshot, and every reader that
    short-circuits on the marker then answered APPROVED while the newest verdict
    was CHANGES. No delete ordering fixes that — the loser of the race is
    whoever writes last, and correctness cannot depend on who that is.

    So the cache carries the digest of the shard names it folded, and a reader
    recomputes it from the CURRENT listing. A cache written from a stale
    snapshot carries a stale digest and is ignored by construction, whenever it
    was written. Validation replaces ordering.

    Names are sorted, so two hosts fingerprinting one directory always agree.
    """
    items = sorted(str(n) for n in (names or []) if str(n).endswith(".md"))
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16]


def evidence_is_immutable(names: Any) -> bool:
    """May a NAME digest fingerprint this evidence at all?

    Only if every shard carries an APPEND-ONLY name. The plain
    ``<head>--<reviewer>.md`` form is permanently supported and hand-writable,
    so it can be REWRITTEN IN PLACE: its content changes and its name does not.
    A name digest cannot detect that, so a cache bound to one answers APPROVED
    from a shard that now reads CHANGES (codex-reviewer, 595 r5).

    This store exposes no etag, no version and no content hash, and its listing
    renders mtimes at one-minute resolution on a 12-hour clock — there is no
    identity here strong enough to bind a mutable shard. The honest answer is to
    not bind one: append-only directories keep the fast path, and a directory
    holding any mutable shard is folded for real, every time. Failing closed
    costs reads; failing open costs a wrong verdict.

    An EMPTY directory is not immutable-safe either: a cache claiming APPROVED
    over zero shards summarises nothing this build can re-derive.
    """
    mds = [str(n) for n in (names or []) if str(n).endswith(".md")]
    return bool(mds) and all(_APPEND_SUFFIX.search(n[:-3]) for n in mds)


#: :func:`settle_shortcircuit` answers.
SETTLE_NO = "no"
SETTLE_CACHE = "cache"
SETTLE_MERGED = "merged"


def settle_shortcircuit(marker_fm: Any, names: Any) -> str:
    """May a reader skip the fold on this ``.settled``, given these shard names?

    ONE decision function, because there is more than one reader: the register
    projection and the fan-out obligation scan both short-circuit on this marker,
    and a rule that lives in one of them is a rule the other silently lacks.

    - ``merged`` — ``state: MERGED`` is merge EVIDENCE, not a recomputable
      tally. It records that a PR landed, which no verdict set can contradict,
      so it short-circuits unconditionally.
    - ``cache``  — ``state: APPROVED`` whose ``evidence`` digest both EXISTS and
      matches a recomputation over the current listing, in a directory where a
      name digest is a valid fingerprint at all.
    - ``no``     — everything else: pre-binding markers carrying no digest,
      stale digests, unreadable markers, and any directory with a mutable shard.
    """
    fm = marker_fm if isinstance(marker_fm, dict) else {}
    state = str(fm.get("state") or "")
    if state == "MERGED":
        return SETTLE_MERGED
    if state != APPROVED:
        return SETTLE_NO
    stamped = str(fm.get("evidence") or "")
    if not stamped or not evidence_is_immutable(names):
        return SETTLE_NO
    return SETTLE_CACHE if stamped == evidence_digest(names) else SETTLE_NO


def settled_marker_fields(*, state: str, ts: str,
                          evidence: Optional[str] = None,
                          merge_sha: Optional[str] = None,
                          tally: Optional[dict[str, Any]] = None) -> dict:
    """The ``.settled`` frontmatter, composed in ONE place.

    Both writers — the read fold's cache and the projection's — render through
    here. The projection used to compose its own dict and omit ``evidence``, so
    every marker it wrote was untrusted by its own reader on the very next pass:
    the cache could never hit, and the write was pure cost (codex-reviewer,
    595 r5). A field one reader requires cannot be optional at one of two write
    sites.
    """
    fields: dict = {"schema": "review-settled/v1", "state": state, "ts": ts}
    if merge_sha:
        fields["merge_sha"] = merge_sha
    else:
        # BINDS THE CACHE TO ITS EVIDENCE. A reader recomputes this from the
        # CURRENT listing, so a cache written from a stale snapshot carries a
        # stale digest and is ignored by construction, whenever it was written.
        fields["evidence"] = evidence or ""
    if tally is not None:
        # The OKF renderer is intentionally shallow, so nested lists/maps must
        # travel as deterministic JSON. This additive v1 field is ignored by
        # older readers; v3 projections use it to recover a complete direct-
        # query row without reopening every shard.
        fields["tally_json"] = json.dumps(
            tally, separators=(",", ":"), sort_keys=True,
            ensure_ascii=False,
        )
    return fields
