"""Generic media CSV — manual import plugin."""
from __future__ import annotations

from datetime import timezone as _timezone
from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep
from fulcra_csv import ColumnMap

from .. import library
from ..fulcra import FulcraClient
from ..importers.generic_csv import _FP_AUTO, parse_media_csv
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import (
    CATEGORY_TO_CANONICAL,
    DURATION_SPEC,
    ensure_media_def,
    import_events,
)


# Shared duration-annotation spec shape used by all three category branches.
# All typed-media definitions share the same structure; category is expressed
# only via the canonical_name argument passed to the resolver.
_GENERIC_DURATION_SPEC: dict = DURATION_SPEC


def _run_generic_csv(ctx: RunContext) -> None:
    """Import an arbitrary CSV (IFTTT, Pipedream, manual export) as Watched/Listened/Read.

    All parameters are read from ctx.config.  Required keys: path, service,
    category.  Optional keys mirror the CLI flags for import generic-csv with
    the same defaults.

    Column-map keys (all optional, CLI defaults):
      ts_col        — timestamp column name (default: "timestamp")
      title_col     — title column name (default: "title")
      subtitle_col  — subtitle/artist column name (default: "artist")
      id_col        — per-content id column name (default: "id")
      duration_col  — duration-in-seconds column name (default: None)
      end_col       — explicit end_time column name (default: None)

    Other optional keys:
      tz            — IANA timezone name for naive timestamps (default: "UTC")
      confidence    — timestamp_confidence value (default: "medium")
      fingerprint   — content fingerprint kind: "auto", "none", or an explicit
                      kind string such as "music", "movie" (default: "auto")
    """
    # --- Required parameters ------------------------------------------------
    path_raw = ctx.config.get("path")
    if not path_raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )

    # --- Optional column-map parameters (CLI defaults) ----------------------
    ts_col: str = ctx.config.get("ts_col", "timestamp")
    title_col: str = ctx.config.get("title_col", "title")
    subtitle_col: str = ctx.config.get("subtitle_col", "artist")
    id_col: str = ctx.config.get("id_col", "id")
    duration_col: str | None = ctx.config.get("duration_col", None)
    end_col: str | None = ctx.config.get("end_col", None)

    # --- Other optional parameters ------------------------------------------
    tz_name: str = ctx.config.get("tz", "UTC")
    confidence: str = ctx.config.get("confidence", "medium")
    fingerprint: str = ctx.config.get("fingerprint", "auto")

    # --- Build ColumnMap (mirror CLI's subtitle_col or None / id_col or None) -
    cm = ColumnMap(
        timestamp=ts_col,
        title=title_col,
        subtitle=subtitle_col or None,
        source_id=id_col or None,
        duration_seconds=duration_col,
        end_time=end_col,
    )

    # --- Resolve timezone (CLI shortcut: "UTC" → timezone.utc, else ZoneInfo) -
    if tz_name == "UTC":
        tz = _timezone.utc
    else:
        tz = ZoneInfo(tz_name)

    # --- Map fingerprint string → fingerprint_kind argument -----------------
    # Mirrors the CLI's two-step mapping exactly:
    #   fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    #   fp_arg  = _FP_AUTO if fingerprint == "auto" else fp_kind
    fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    fp_arg = _FP_AUTO if fingerprint == "auto" else fp_kind

    # --- Ensure the annotation definition is known before importing ----------
    # The category (watched/listened/read) is set per-instance via plugin
    # config, so we look it up at run-time and call the resolver with the
    # matching canonical name.  On a fresh install (machine 2) the target
    # field in media state may be absent; the resolver adopts the existing
    # definition rather than creating a duplicate.
    canonical = CATEGORY_TO_CANONICAL[category]
    target_field = f"{category}_definition_id"
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr=target_field,
                     spec=_GENERIC_DURATION_SPEC, canonical_name=canonical,
                     state_save=_state_save)

    # --- Resolve path, parse, and import ------------------------------------
    resolved = library.resolve(path_raw)
    events = list(parse_media_csv(
        resolved,
        service=service,
        category=category,
        column_map=cm,
        tz=tz,
        confidence=confidence,
        fingerprint_kind=fp_arg,
    ))
    import_events(
        ctx, events, service,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="generic-csv",
    name="Generic media CSV",
    kind="manual",
    run=_run_generic_csv,
    description=(
        "Imports any CSV of media events — IFTTT exports, Pipedream "
        "dumps, hand-crafted spreadsheets. You configure which columns "
        "hold the timestamp, title, and subtitle, plus a service tag "
        "and category (watched / listened / read). Manual."
    ),
    default_interval=None,
    category="other",
    # canonical_definition_name is intentionally absent: the canonical identity
    # depends on the runtime config value of "category", not on the Plugin
    # definition itself.  See CATEGORY_TO_CANONICAL and _run_generic_csv.
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="CSV file path",
            kind="path",
            help="Local path to the CSV file you want to import.",
        ),
        Setting(
            key="service",
            label="Service tag",
            kind="text",
            help=(
                "Short identifier we'll attach to each event "
                "(e.g. 'ifttt', 'manual', 'sheets')."
            ),
        ),
        Setting(
            key="category",
            label="Category",
            kind="enum",
            enum_values=("watched", "listened", "read"),
            help=(
                "Which canonical annotation to write to — 'watched' "
                "for video, 'listened' for audio, 'read' for text/books."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Got a CSV of media events from somewhere we don't "
                "natively support? Upload it here. You'll tell us which "
                "columns hold the timestamp, title, and subtitle "
                "(advanced options live in `config.toml` — defaults "
                "match common IFTTT/Pipedream exports). Each row "
                "becomes a Fulcra annotation."
            ),
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your CSV",
            body_md=(
                "Pick the CSV file. Defaults assume columns named "
                "`timestamp`, `title`, `artist`, and `id` — tweak "
                "`ts_col`, `title_col`, etc. in `config.toml` later if "
                "yours differs."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="input",
            title="Tag and categorise",
            body_md=(
                "Pick a **service** tag (a short label that identifies "
                "where this CSV came from) and a **category** — "
                "'watched' for video, 'listened' for audio, 'read' for "
                "books or articles."
            ),
            settings_keys=("service", "category"),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write these events?",
            body_md=(
                "We can write to your existing Watched/Listened/Read "
                "annotation (whichever matches your category) or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Generic CSV is configured. Click **Run now** from the "
                "dashboard to import the file. Re-upload a fresh CSV "
                "any time you want to import new rows."
            ),
        ),
    ),
)
