"""fulcra-collect plugin: import Day One entries (manual).

run(ctx) reads its source from ctx.config — either {"local_db": true}
or {"path": "<export .zip or folder>"} — runs the read -> select ->
convert -> run_import pipeline, and reports counts via ctx.progress.
A manual plugin: the hub fires it only on `fulcra-collect run dayone`.
"""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.plugin import Plugin, RunContext

from .client import DayOneFulcraClient
from .convert import to_event
from .filter import select
from .readers import read


def run(ctx: RunContext) -> None:
    local_db = bool(ctx.config.get("local_db", False))
    path_setting = ctx.config.get("path")
    if not local_db and not path_setting:
        raise RuntimeError(
            "dayone: set either `local_db = true` or `path = \"<export>\"` in "
            "this plugin's settings (config.toml [plugin_settings.dayone])"
        )
    source = Path(path_setting) if path_setting else None
    db_path = Path(ctx.config["db_path"]) if ctx.config.get("db_path") else None

    entries = read(source, local_db=local_db, db_path=db_path)
    selected = select(entries)  # plan 1a imports all entries; filters are a 1b concern
    ctx.progress(stage="read", count=len(selected))
    if not selected:
        return

    events = [to_event(e) for e in selected]
    client = DayOneFulcraClient()
    definition_id = client.ensure_journal_definition()
    tag_names = sorted({t for e in selected for t in getattr(e, "tags", ())})
    tag_id_for = {name: client.ensure_tag(name) for name in tag_names}
    result = client.run_import(events, definition_id=definition_id,
                               tag_id_for=tag_id_for)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)


PLUGIN = Plugin(
    id="dayone",
    name="Day One journal",
    kind="manual",
    run=run,
    description=(
        "Imports your Day One journal entries as moment annotations. Manual — "
        "point this at your Day One export."
    ),
    category="journal",
)
