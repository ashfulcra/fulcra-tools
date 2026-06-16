"""Dependency-injected CLI surface for fulcra-vault."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from typing import TextIO

from .frontmatter import parse_note
from .links import backlinks_for, build_index, index_json
from .locks import LockError, locked
from .map import check_budget, render_hot, render_map, select_hot_items
from .schema import StructureSpec, VaultMeta, normalize_note_path
from .sections import append_log, replace_owned_section
from .store import FulcraVaultStore, MissingFileError, StoreError


def run(argv: list[str] | None = None, *, store=None, now: datetime | None = None,
        stdin: TextIO | None = None, stdout: TextIO | None = None,
        stderr: TextIO | None = None) -> int:
    argv = list(argv or [])
    store = store or FulcraVaultStore()
    now = now or datetime.now(timezone.utc)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code)
    try:
        return args.func(args, store, now, stdin, stdout, stderr)
    except (StoreError, LockError, ValueError) as e:
        print(f"fulcra-vault: {e}", file=stderr)
        return 2


def main() -> int:
    return run(sys.argv[1:])


def cmd_read(args, store, now, stdin, stdout, stderr) -> int:
    try:
        text = store.read_text(_note_remote(args.note))
    except (FileNotFoundError, MissingFileError):
        print("fulcra-vault: not onboarded or note missing", file=stderr)
        return 0
    stdout.write(text)
    if args.with_backlinks:
        index = _load_index(store)
        backs = backlinks_for(index, args.note)
        if backs:
            stdout.write("\n## Backlinks\n")
            for path in backs:
                stdout.write(f"- [[{path.removesuffix('.md')}]]\n")
    return 0


def cmd_write_section(args, store, now, stdin, stdout, stderr) -> int:
    note = normalize_note_path(args.note)
    _ensure_not_excluded(store, note)
    remote = _note_remote(note)
    original, pre_stat = _read_with_stat(store, remote)
    parse_note(original)
    body = stdin.read()
    with locked(store, note, holder=args.agent, now=now):
        _abort_if_changed(store, remote, pre_stat)
        changed = replace_owned_section(
            original,
            args.section,
            args.agent,
            body,
            force=args.force,
        )
        parse_note(changed)
        store.write_text(remote, changed)
        _append_vault_log(store, f"write-section {note} {args.section}", now, args.agent)
    print(f"updated {note}", file=stderr)
    return 0


def cmd_append_log(args, store, now, stdin, stdout, stderr) -> int:
    note = normalize_note_path(args.note)
    _ensure_not_excluded(store, note)
    remote = _note_remote(note)
    original, pre_stat = _read_with_stat(store, remote)
    parse_note(original)
    with locked(store, note, holder=args.agent, now=now):
        _abort_if_changed(store, remote, pre_stat)
        changed = append_log(original, args.entry, now, args.agent)
        parse_note(changed)
        store.write_text(remote, changed)
        _append_vault_log(store, f"append-log {note}", now, args.agent)
    print(f"updated {note}", file=stderr)
    return 0


def cmd_backlinks(args, store, now, stdin, stdout, stderr) -> int:
    index = _load_index(store)
    for path in backlinks_for(index, args.note):
        stdout.write(path + "\n")
    return 0


def cmd_reindex(args, store, now, stdin, stdout, stderr) -> int:
    notes = _read_note_map(store)
    body = index_json(notes) + "\n"
    store.write_text("/vault/.index/links.json", body)
    _append_vault_log(store, "reindex", now, args.agent)
    print("reindexed", file=stderr)
    return 0


def cmd_map(args, store, now, stdin, stdout, stderr) -> int:
    meta = _load_meta(store)
    notes = _read_note_map(store)
    links = build_index(notes)
    rendered_map = render_map(meta.spec, notes, links)
    hot = render_hot(select_hot_items(notes, links, now))
    check_budget(rendered_map, max_words=args.max_map_words, label="MAP")
    check_budget(hot, max_words=args.max_hot_words, label="HOT")
    if args.check:
        print("MAP/HOT render check passed", file=stderr)
        return 0
    store.write_text("/vault/MAP.md", rendered_map)
    store.write_text("/vault/HOT.md", hot)
    _append_vault_log(store, "map refresh", now, args.agent)
    print("refreshed MAP.md and HOT.md", file=stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fulcra-vault")
    sub = parser.add_subparsers(dest="cmd", required=True)
    read = sub.add_parser("read")
    read.add_argument("note")
    read.add_argument("--with-backlinks", action="store_true")
    read.set_defaults(func=cmd_read)

    write = sub.add_parser("write-section")
    write.add_argument("note")
    write.add_argument("--section", required=True)
    write.add_argument("--agent", required=True)
    write.add_argument("--force", action="store_true")
    write.set_defaults(func=cmd_write_section)

    log = sub.add_parser("append-log")
    log.add_argument("note")
    log.add_argument("--entry", required=True)
    log.add_argument("--agent", required=True)
    log.set_defaults(func=cmd_append_log)

    backlinks = sub.add_parser("backlinks")
    backlinks.add_argument("note")
    backlinks.set_defaults(func=cmd_backlinks)

    reindex = sub.add_parser("reindex")
    reindex.add_argument("--agent", default="fulcra-vault")
    reindex.set_defaults(func=cmd_reindex)

    map_cmd = sub.add_parser("map")
    map_cmd.add_argument("--check", action="store_true")
    map_cmd.add_argument("--agent", default="fulcra-vault")
    map_cmd.add_argument("--max-map-words", type=int, default=1200)
    map_cmd.add_argument("--max-hot-words", type=int, default=500)
    map_cmd.set_defaults(func=cmd_map)
    return parser


def _load_meta(store) -> VaultMeta:
    data = json.loads(store.read_text("/vault/meta.json"))
    if not isinstance(data, dict):
        raise ValueError("meta.json must be a JSON object")
    try:
        spec = StructureSpec.from_dict(data["spec"])
        return VaultMeta(
            spec=spec,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            schema_version=data.get("schema_version", 1),
        )
    except KeyError as e:
        # surface as ValueError so run()'s handler returns rc 2 instead of a traceback
        raise ValueError(f"meta.json missing required key: {e}") from e


def _ensure_not_excluded(store, note: str) -> None:
    meta = _load_meta(store)
    for excluded in meta.spec.exclusions:
        prefix = excluded.rstrip("/") + "/"
        if note == excluded or note.startswith(prefix):
            raise ValueError(f"excluded path: {note}")


def _read_with_stat(store, remote: str) -> tuple[str, dict | None]:
    before = store.stat(remote)
    text = store.read_text(remote)
    return text, before


def _abort_if_changed(store, remote: str, pre_stat: dict | None) -> None:
    if store.stat(remote) != pre_stat:
        raise ValueError(f"{remote} changed since read; retry")


def _append_vault_log(store, action: str, now: datetime, agent: str) -> None:
    path = "/vault/LOG.md"
    try:
        current = store.read_text(path)
    except (FileNotFoundError, MissingFileError):
        current = ""
    line = f"- {now.isoformat()} {agent}: {action}\n"
    store.write_text(path, current + line)


def _read_note_map(store) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in store.list("vault"):
        if not path.endswith(".md"):
            continue
        if path.startswith("/vault/.locks/") or path.startswith("/vault/.index/"):
            continue
        if path in {"/vault/MAP.md", "/vault/HOT.md", "/vault/LOG.md"}:
            continue
        rel = path.removeprefix("/vault/")
        out[rel] = store.read_text(path)
    return out


def _load_index(store) -> dict:
    try:
        return json.loads(store.read_text("/vault/.index/links.json"))
    except (FileNotFoundError, MissingFileError, json.JSONDecodeError):
        return build_index(_read_note_map(store))


def _note_remote(note: str) -> str:
    return "/vault/" + normalize_note_path(note)
