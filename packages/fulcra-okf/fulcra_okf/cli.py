"""fulcra-okf command-line interface: validate / info / fmt."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import sys

from .bundle import Bundle, RESERVED_NAMES, render_concept
from .validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fulcra-okf", description="OKF v0.1 tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="check §9 conformance")
    p_val.add_argument("dir")
    p_val.add_argument("--strict", action="store_true")
    p_val.add_argument("--json", action="store_true")

    p_info = sub.add_parser("info", help="summarize a bundle")
    p_info.add_argument("dir")

    p_fmt = sub.add_parser("fmt", help="normalize frontmatter")
    p_fmt.add_argument("dir")
    p_fmt.add_argument("--check", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        return _cmd_validate(args)
    if args.cmd == "info":
        return _cmd_info(args)
    return _cmd_fmt(args)


def _cmd_validate(args) -> int:
    bundle = Bundle.load_dir(args.dir, lenient=True)
    report = validate(bundle, strict=args.strict)
    if args.json:
        print(json.dumps({
            "conformant": report.conformant,
            "findings": [vars(f) for f in report.findings],
        }, indent=2))
    else:
        for f in report.findings:
            print(f"{f.severity.upper()} {f.path}: {f.code} — {f.message}")
        print("CONFORMANT" if report.conformant else "NOT CONFORMANT")
    return 0 if report.conformant else 1


def _cmd_info(args) -> int:
    bundle = Bundle.load_dir(args.dir, lenient=True)
    ids = set(bundle.concepts)
    types = Counter(c.type for c in bundle.concepts.values())
    broken = sum(
        1 for c in bundle.concepts.values() for t in c.links() if t not in ids
    )
    reserved = sorted(
        p.name for p in Path(args.dir).rglob("*.md") if p.name in RESERVED_NAMES
    )
    print(f"concepts: {len(ids)}")
    print(f"okf_version: {bundle.okf_version}")
    print(f"reserved files: {reserved}")
    print(f"broken links: {broken}")
    print("types:")
    for type_name, count in sorted(types.items()):
        print(f"  {type_name or '(empty)'}: {count}")
    return 0


def _cmd_fmt(args) -> int:
    src = Path(args.dir)
    bundle = Bundle.load_dir(src, lenient=True)
    changed: list[str] = []
    errors: list[str] = []
    for rel, message in bundle.parse_errors:
        print(f"error: {rel}: {message}", file=sys.stderr)
        errors.append(rel)
    for concept in bundle.concepts.values():
        try:
            rendered = render_concept(concept)
        except Exception as exc:  # noqa: BLE001
            rel = concept.id + ".md"
            print(f"error: {rel}: {exc}", file=sys.stderr)
            errors.append(rel)
            continue
        target = src / (concept.id + ".md")
        if target.read_text() != rendered:
            changed.append(concept.id + ".md")
            if not args.check:
                target.write_text(rendered)
    if errors:
        return 1
    if args.check:
        for rel in changed:
            print(f"would reformat: {rel}")
        return 1 if changed else 0
    for rel in changed:
        print(f"reformatted: {rel}")
    return 0
