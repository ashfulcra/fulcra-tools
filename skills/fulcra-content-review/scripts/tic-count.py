#!/usr/bin/env python3
"""Measured AI-tell sweep for prose (fulcra-content-review §2).

Counts the telltale patterns that make text read as machine-written, so editing
is grounded in numbers instead of taste. Run before and after an edit; the diff
is the edit's receipt.

Usage: tic-count.py <file.html|file.md|file.txt>
Exit code 0 always — this is a meter, not a gate; thresholds live in SKILL.md.
"""
import html
import re
import sys


def main(path: str) -> None:
    raw = open(path, encoding="utf-8").read()
    # Strip HTML tags, then markdown syntax; both are harmless on plain text.
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    txt = re.sub(r"[#*_`>\[\]()|]", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    words = max(1, len(txt.split()))

    def count(pattern: str) -> int:
        return len(re.findall(pattern, txt, re.I))

    rows = [
        ("em-dashes", count("—"), "< 0.15/100w"),
        ("'rather than'", count(r"\brather than\b"), "< 8"),
        ("', not X' antithesis", count(r", not [a-z]"), "<= 3"),
        ("'which is the/what/why'", count(r"which is (the|what|why|exactly)"), "< 4"),
        ("ceremony ('worth noting/being clear/saying')",
         count(r"\bworth (noting|being clear|saying|stating|remembering)\b"), "~0"),
        ("'genuinely/precisely/exactly'",
         count(r"\b(genuinely|precisely|exactly)\b"), "< 5"),
        ("'not only... but'", count(r"not only\b"), "~0"),
        ("triad-comma runs (a, b, and c adjectives)",
         count(r"\b\w+, \w+,? and \w+\b"), "eyeball"),
    ]

    print(f"words: {words}")
    for name, n, target in rows:
        rate = f"  ({n / words * 100:.2f}/100w)" if name == "em-dashes" else ""
        print(f"{name}: {n}{rate}   target {target}")

    # Aphorism-closer heuristic: short punchy final sentence per paragraph.
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw) if len(p.split()) > 40]
    closers = 0
    for p in paras:
        last = re.split(r"(?<=[.!?]) ", re.sub(r"<[^>]+>", " ", p).strip())[-1]
        if 4 <= len(last.split()) <= 14 and ("—" in last or ", not " in last
                                             or last.rstrip(".").endswith(("itself", "all", "own"))):
            closers += 1
    print(f"aphorism-ish closers (heuristic): {closers} of {len(paras)} long paragraphs")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
