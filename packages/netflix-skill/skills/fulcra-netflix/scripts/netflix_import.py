#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Import a Netflix viewing-history CSV into a Fulcra Watched annotation.

Self-contained (PEP 723): run with `uv run netflix_import.py <csv> --json`.
Parsing logic and deterministic source-id schemes are ported verbatim from
fulcra-media (packages/media-helpers/fulcra_media/importers/netflix.py) so
records dedup perfectly against fulcra-media imports of the same history.
Wire format mirrors fulcra-common wire.py; dedup is source-id based.
"""
from __future__ import annotations

API_BASE = "https://api.fulcradynamics.com"
DEF_NAME = "Watched"
DEF_MARKER = "com.fulcradynamics.annotation.media.watched"
INGEST_VERSION = 2  # bump ONLY with a coordinated det-id prefix change
