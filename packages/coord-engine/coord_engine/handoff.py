"""Validation for handoff parks — the five-section gate.

WHY THIS EXISTS. `docs/coord/CHECKPOINT-HANDOFF.md` defines two tiers: the wake
snapshot (short, routine) and the handoff park (the full five-section form a
successor with zero context resumes from). Until now the standard was doctrine
and the engine enforced nothing, so the only thing standing between a fleet and
an unresumable handoff was whether the outgoing agent remembered the form while
its context ran out — which is precisely the moment it will not.

THE FAILURE THIS IS BUILT AGAINST is not a missing section, it is a **lying
pointer**. coord-boss's 2026-08-06 takeover resumed a checkpoint whose artifact
list pointed at a "Cold start" section of `_coord/agents/coord-boss/harness.md` (on the team store) that
did not exist on main. Every section was present; the handoff still failed,
because the standard's real requirement — *"never point at something that does
not exist"* — is the one a presence check cannot see. So artifacts get a LIVE
resolve, and an artifact naming an anchor gets that anchor checked too.

WHAT IS AND IS NOT MACHINE-CHECKABLE, stated plainly because the gate's value
depends on not overclaiming. Structure is checkable: a section is present, a
decision carries an expiry, an artifact resolves. QUALITY is not. Nothing here
can tell a real objective from a plausible sentence, and a gate that pretended
otherwise would fail good handoffs and pass fluent empty ones. The checks below
are floors, deliberately — they catch the handoff written in a hurry, not the
one written in bad faith.

SCOPE: this module is pure. All I/O (store reads, repo reads) arrives through an
injected resolver, so the rules are testable without a store and the same rules
apply wherever a caller can resolve a pointer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple, Optional

#: The five required sections, in the order the standard lists them. Findings
#: are reported in this order so a reader fixes a handoff top-down rather than
#: playing whack-a-mole with one error at a time.
REQUIRED_SECTIONS = (
    "objective",
    "decisions",
    "next_actions",
    "open_questions",
    "artifacts",
)

#: A decision must say when its authority lapses. An ISO date is the common
#: case ("expires 2026-08-09"); the explicit-permanence words exist so that
#: standing doctrine is not forced to invent a fake expiry, which would be a
#: worse outcome than no gate at all.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_PERMANENT = re.compile(
    r"\b(no expiry|never expires|permanent|standing law|indefinite)\b", re.I)

#: An open question must name who owes the answer. Either an explicit owner
#: clause or an agent-shaped token is accepted — the point is that a successor
#: can tell who to ask, not that a particular syntax was used.
_OWNER = re.compile(
    r"(\bowner\s*[:=]|\bowes?\b|\bawait(?:ing)?\b|\bask\b|\bdeferred to\b"
    r"|\b(?:ash|operator|coord-[a-z0-9-]+|codex-[a-z0-9-]+)\b)", re.I)

#: A next action must be startable "without archaeology" — so it has to carry at
#: least one concrete identifier. A path, a PR number, or a slug-shaped token.
#: Two-segment hyphenated tokens count, because this fleet's real identifiers
#: look like that (``needs-me``, ``build-lane``, ``bus-v3``). The cost is that a
#: hyphenated English word can pass — accepted deliberately: a gate that wrongly
#: REFUSES a good handoff is one people route around, and a false pass on one
#: entry is cheaper than that.
_IDENTIFIER = re.compile(
    r"(\bPR\s*#?\d+\b|\b[a-f0-9]{7,40}\b|[\w.-]+/[\w./-]+|\b[a-z0-9]+(?:-[a-z0-9]+)+\b)",
    re.I)

#: An explicit "there are none" for a section that may legitimately be empty.
#: Without this, requiring a non-empty open-questions list would push agents to
#: invent a question to satisfy the gate — buying a filled field and losing the
#: signal it carried.
_EXPLICIT_NONE = re.compile(r"^\s*(none|n/a|nothing( open)?|no open questions)"
                            r"\b[.\s]*$", re.I)

#: Free-text floor. Short enough that a real one-liner passes, long enough that
#: a bare label ("decided", "see above") does not. A floor, not a quality bar.
MIN_ENTRY_CHARS = 24

#: ``path#anchor`` and the parenthetical form the standard's own worked example
#: uses: ``_coord/agents/coord-boss/harness.md (Cold start section)``.
_ANCHOR_PAREN = re.compile(r"^(?P<path>[^()]+?)\s*\(\s*(?P<anchor>.+?)\s*\)\s*$")


class Finding(NamedTuple):
    """One reason a handoff is not resumable. ``section`` is always one of
    REQUIRED_SECTIONS so a caller can group without parsing prose."""
    section: str
    detail: str


class Artifact(NamedTuple):
    """An artifact reference split into what a resolver needs."""
    raw: str
    path: str
    anchor: Optional[str]


#: A resolver answers "does this pointer resolve RIGHT NOW": it takes an
#: :class:`Artifact` and returns ``(ok, reason)``. ``reason`` is only read when
#: ``ok`` is False. A resolver that cannot tell must return False with a reason
#: saying so — UNKNOWN is not "fine", the same rule the rest of the engine
#: follows, and an unverifiable pointer is exactly what broke the takeover.
Resolver = Callable[[Artifact], "tuple[bool, str]"]


def parse_artifact(raw: Any) -> Optional[Artifact]:
    """Split ``path#anchor`` / ``path (Anchor)`` into parts, or None if unusable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    m = _ANCHOR_PAREN.match(text)
    if m:
        path = m.group("path").strip()
        anchor = m.group("anchor").strip()
        return Artifact(text, path, anchor or None) if path else None
    if not re.search(r"[A-Za-z0-9]", text):
        return None
    if "#" in text:
        path, _, anchor = text.partition("#")
        path, anchor = path.strip(), anchor.strip()
        if path:
            return Artifact(text, path, anchor or None)
        return None
    return Artifact(text, text, None)


def _entries(value: Any) -> list[str]:
    """Normalize a snapshot list field to non-blank strings."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        return []
    return [e.strip() for e in value
            if isinstance(e, str) and e.strip()]


def _check_objective(snapshot: dict[str, Any]) -> list[Finding]:
    objective = snapshot.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return [Finding("objective", "missing — a handoff must state who you "
                                     "are and what the system state IS")]
    if len(objective.strip()) < MIN_ENTRY_CHARS:
        return [Finding("objective",
                        f"too thin to resume from ({len(objective.strip())} "
                        f"chars) — the standard asks for the state you left "
                        f"PLUS the evidence one-liner")]
    return []


def _check_decisions(snapshot: dict[str, Any]) -> list[Finding]:
    entries = _entries(snapshot.get("decisions"))
    if not entries:
        return [Finding("decisions", "missing — standing law your successor "
                                     "must not re-litigate")]
    out: list[Finding] = []
    for entry in entries:
        if not (_ISO_DATE.search(entry) or _PERMANENT.search(entry)):
            out.append(Finding("decisions",
                               f"no expiry: {entry[:60]!r} — say a date, or say "
                               f"it is standing law explicitly"))
        elif len(entry) < MIN_ENTRY_CHARS:
            # Only checked once an expiry exists, so one entry yields one
            # finding: a bare label with no expiry has one root problem.
            out.append(Finding("decisions",
                               f"no rationale: {entry[:60]!r} — a rule without "
                               f"its wound gets re-broken"))
    return out


def _check_next_actions(snapshot: dict[str, Any]) -> list[Finding]:
    entries = _entries(snapshot.get("next_actions"))
    if not entries:
        return [Finding("next_actions",
                        "missing — ordered, concrete, startable without you")]
    return [Finding("next_actions",
                    f"no identifier: {entry[:60]!r} — name the slug, PR number "
                    f"or exact path so it can be started without archaeology")
            for entry in entries if not _IDENTIFIER.search(entry)]


def _check_open_questions(snapshot: dict[str, Any]) -> list[Finding]:
    entries = _entries(snapshot.get("open_questions"))
    if not entries:
        return [Finding("open_questions",
                        "missing — if nothing is genuinely undecided, say "
                        "'none' explicitly; an empty field cannot be told "
                        "apart from a forgotten one")]
    if len(entries) == 1 and _EXPLICIT_NONE.match(entries[0]):
        return []
    return [Finding("open_questions",
                    f"no owner: {entry[:60]!r} — name who owes the answer")
            for entry in entries if not _OWNER.search(entry)]


def _check_artifacts(snapshot: dict[str, Any],
                     resolve: Optional[Resolver]) -> list[Finding]:
    entries = _entries(snapshot.get("artifacts"))
    if not entries:
        return [Finding("artifacts",
                        "missing — the cold-start reading list is what makes "
                        "the checkpoint a handoff rather than a diary entry")]
    out: list[Finding] = []
    for entry in entries:
        art = parse_artifact(entry)
        if art is None:
            out.append(Finding("artifacts", f"unparseable: {entry[:60]!r}"))
            continue
        if resolve is None:
            # No resolver is NOT a pass. The live check is the point of this
            # section; skipping it silently would ship the exact false-pass
            # class the standard was written after.
            out.append(Finding("artifacts",
                               f"NOT VERIFIED: {art.raw[:60]!r} — no resolver "
                               f"available, so this pointer is UNKNOWN"))
            continue
        ok, reason = resolve(art)
        if not ok:
            out.append(Finding("artifacts",
                               f"does not resolve: {art.raw[:60]!r} — {reason}"))
    return out


def validate(snapshot: dict[str, Any], *,
             resolve: Optional[Resolver] = None) -> list[Finding]:
    """Every reason ``snapshot`` is not a resumable handoff, in section order.

    Empty list means it passes. ALL findings are returned rather than the first:
    naming one missing section at a time turns fixing a handoff into an
    iterative guessing game at exactly the moment the author is out of context.
    """
    if not isinstance(snapshot, dict):
        return [Finding(s, "snapshot is not a document") for s in REQUIRED_SECTIONS]
    return (_check_objective(snapshot)
            + _check_decisions(snapshot)
            + _check_next_actions(snapshot)
            + _check_open_questions(snapshot)
            + _check_artifacts(snapshot, resolve))


def format_findings(findings: list[Finding]) -> str:
    """The fail-closed message: what is wrong, grouped by section, in order."""
    if not findings:
        return ""
    lines = ["handoff park REFUSED — checkpoint is not resumable "
             "(docs/coord/CHECKPOINT-HANDOFF.md):"]
    for section in REQUIRED_SECTIONS:
        for f in findings:
            if f.section == section:
                lines.append(f"  [{section}] {f.detail}")
    lines.append("NOTHING WAS WRITTEN. Fix the above and re-run, or use "
                 "`continuity snapshot` if this is a routine wake save.")
    return "\n".join(lines)


def store_resolver(transport: Any, team: str) -> Resolver:
    """Resolve artifacts against the File Store, then the repo working tree.

    An artifact naming an anchor must have that anchor present in the resolved
    document — the takeover failure was a real file whose named section did not
    exist, so file-exists alone would have passed the very case this is for.
    """
    def _resolve(art: Artifact) -> "tuple[bool, str]":
        text, where = _read_candidate(transport, team, art.path)
        if text is None:
            return False, where
        if art.anchor and not _anchor_present(text, art.anchor):
            return False, (f"{where} exists but has no section matching "
                           f"{art.anchor!r}")
        return True, where
    return _resolve


def _read_candidate(transport: Any, team: str, path: str
                    ) -> "tuple[Optional[str], str]":
    """``(text, where)`` — store first, then the REPOSITORY. ``where`` doubles
    as the failure reason when text is None.

    The working-tree leg is confined to the discovered repository root, and it
    rejects absolute paths and traversal. An artifact must be store-carried or
    repo-carried: those are the two things a successor will have. A path that
    resolves only because THIS host happens to have that file — ``/etc/hosts``,
    ``../../something-local`` — is a lying pointer that merely lies later, which
    is the exact class this gate exists to reject (codex-reviewer, round 2).
    """
    candidates = [path] if path.startswith("team/") else [
        f"team/{team}/{path}", path]
    for candidate in candidates:
        try:
            raw = transport.read(candidate)
        except Exception:
            raw = None
        if raw is not None:
            return raw, f"store:{candidate}"
    local = _repo_relative(path)
    if local is None:
        return None, ("not found in the store, and not a repository-relative "
                      "path (absolute paths and traversal outside the repo "
                      "are refused — a successor will not have them)")
    try:
        if local.is_file():
            return local.read_text(encoding="utf-8", errors="replace"), \
                f"repo:{path}"
    except Exception:
        pass
    return None, "not found in the store or the repository"


def repo_root(start: "Optional[Path]" = None) -> "Optional[Path]":
    """The enclosing git repository root, or None if there is not one.

    Discovered rather than assumed: resolving repo paths against the CURRENT
    WORKING DIRECTORY made the gate's verdict depend on where it was invoked
    from — the same document passed from the repo root and failed from a
    subdirectory.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _repo_relative(path: str) -> "Optional[Path]":
    """``path`` resolved inside the repo, or None if it escapes or there is no
    repo. Absolute paths are refused outright rather than clamped."""
    if os.path.isabs(path):
        return None
    root = repo_root()
    if root is None:
        return None
    try:
        resolved = (root / path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return resolved


def _anchor_present(text: str, anchor: str) -> bool:
    """Is ``anchor`` a heading in ``text``?

    Compares NORMALIZED word sequences for EQUALITY. Normalization drops
    punctuation, case, and the trailing word "section" — a reading list says
    "Cold start section" where the document says "## Cold start", and failing a
    handoff over that difference is a gate nobody trusts.

    Equality, not containment. Round 1 of this gate used subset matching in
    either direction, which meant an artifact naming "Cold start" was satisfied
    by a document whose only heading was "# Start" (codex-reviewer reproduced
    it). A pointer that resolves to the WRONG section is the lying-pointer class
    this whole module exists to reject, so a near-miss must fail.
    """
    wanted = _words(anchor)
    if not wanted:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        if _words(stripped.lstrip("#")) == wanted:
            return True
    return False


def _words(value: str) -> tuple:
    """Normalized word sequence: lowercase, punctuation-free, minus "section".

    A SEQUENCE rather than a set, so "Cold start" and "Start cold" are not the
    same anchor.
    """
    return tuple(w for w in re.split(r"[^a-z0-9]+", value.lower())
                 if w and w != "section")

