"""Deterministic rule derivation from labeled example emails (pure, no I/O).

Given the header records of the operator's ✓ (should-match) and ✗ (should-not)
examples, compute the traits the positives SHARE that the negatives do NOT, and
express them as editable ``Chip``s that assemble into a draft rule dict. No
message body, no network, no logging of content — the route layer passes in
already-extracted header fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_STOPWORDS = {
    "the", "and", "for", "your", "you", "with", "from", "this", "that", "are",
    "was", "has", "have", "our", "new", "get", "please", "hello", "hi", "re",
    "fwd", "fw", "no", "yes", "all", "about", "into", "out",
}


@dataclass
class Chip:
    kind: str          # sender | domain | list | subject_kw | attachment | exclude_domain
    field: str         # match | subject_regex | from_regex | has_attachment
    value: str
    label: str
    on: bool


@dataclass
class DerivationResult:
    chips: list[Chip] = field(default_factory=list)
    draft_rule: dict = field(default_factory=dict)
    needs_refinement: bool = False


def _addr(frm: str) -> str | None:
    m = _ADDR_RE.search(frm or "")
    return m.group(0).lower() if m else None


def _domain(addr: str | None) -> str | None:
    return addr.split("@", 1)[1] if addr and "@" in addr else None


def _subject_tokens(subject: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((subject or "").lower())
            if t not in _STOPWORDS}


def _all_share(records: list[dict], keyfn) -> str | None:
    """The single value shared by EVERY record (else None)."""
    vals = {keyfn(r) for r in records}
    vals.discard(None)
    if len(vals) == 1 and len(records) > 0 and all(keyfn(r) is not None for r in records):
        return next(iter(vals))
    return None


def derive(positives: list[dict], negatives: list[dict]) -> DerivationResult:
    if not positives:
        return DerivationResult(needs_refinement=True)

    pos_addrs = [_addr(r.get("from", "")) for r in positives]
    neg_addrs = {_addr(r.get("from", "")) for r in negatives}
    neg_domains = {_domain(a) for a in neg_addrs}
    neg_lists = {r.get("list_id") for r in negatives}
    neg_tokens: set[str] = set()
    for r in negatives:
        neg_tokens |= _subject_tokens(r.get("subject", ""))

    chips: list[Chip] = []

    # Sender (exact) — preferred; else sender domain.
    shared_addr = _all_share(positives, lambda r: _addr(r.get("from", "")))
    if shared_addr and shared_addr not in neg_addrs:
        chips.append(Chip("sender", "match", f"from:{shared_addr}",
                          f"From {shared_addr}", on=True))
    else:
        shared_domain = _all_share(positives, lambda r: _domain(_addr(r.get("from", ""))))
        if shared_domain:
            on = shared_domain not in neg_domains
            chips.append(Chip("domain", "match", f"from:{shared_domain}",
                              f"From @{shared_domain}", on=on))

    # Mailing list.
    shared_list = _all_share(positives, lambda r: r.get("list_id"))
    if shared_list and shared_list not in neg_lists:
        chips.append(Chip("list", "match", f"list:{shared_list}",
                          f"Mailing list {shared_list}", on=True))

    # Attachment.
    if all(r.get("has_attachment") for r in positives):
        on = not all(r.get("has_attachment") for r in negatives) if negatives else True
        chips.append(Chip("attachment", "match", "has:attachment",
                          "Has an attachment", on=on))

    # Subject keyword shared by all positives, not in any negative.
    common_tokens: set[str] | None = None
    for r in positives:
        toks = _subject_tokens(r.get("subject", ""))
        common_tokens = toks if common_tokens is None else (common_tokens & toks)
    common_tokens = (common_tokens or set()) - neg_tokens
    if common_tokens:
        kw = sorted(common_tokens, key=len, reverse=True)[0]
        chips.append(Chip("subject_kw", "subject_regex", f"(?i){re.escape(kw)}",
                          f"Subject contains '{kw}'", on=True))

    # Negative-only domain → exclusion (tightens when positives don't share it).
    for d in sorted(x for x in neg_domains if x):
        pos_domains = {_domain(a) for a in pos_addrs}
        if d not in pos_domains:
            chips.append(Chip("exclude_domain", "match", f"-from:{d}",
                              f"Exclude @{d}", on=False))

    draft = draft_from_chips(chips)
    needs = not any(c.on for c in chips)
    return DerivationResult(chips=chips, draft_rule=draft, needs_refinement=needs)


def draft_from_chips(chips: list[Chip]) -> dict:
    """Assemble a rule dict (match + post-filters) from the ON chips."""
    match_terms = [c.value for c in chips if c.on and c.field == "match"]
    draft: dict = {"match": " ".join(match_terms).strip()}
    for c in chips:
        if not c.on:
            continue
        if c.field == "subject_regex":
            draft["subject_regex"] = c.value
        elif c.field == "from_regex":
            draft["from_regex"] = c.value
        elif c.field == "has_attachment":
            draft["has_attachment"] = True
    return draft
