"""Structured logging for fulcra-labs (CLAUDE.md requires instrumentation).

Every module gets a component logger via ``get_logger(__name__)``. The CLI
calls ``configure()`` once to attach a single stderr handler with a level
driven by ``$FULCRA_LABS_LOG`` (default INFO; ``debug`` for per-row decisions).
Logs go to stderr so they never pollute the ``--json`` envelope on stdout.

Per the verify-before-ingest posture, every row-level decision (resolve /
convert / range-check / dedupe / verdict) is logged with its reason — the
pipeline never drops a row silently.
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False
_ROOT = "fulcra_labs"


def configure(level: str | None = None) -> None:
    """Attach one stderr handler to the ``fulcra_labs`` logger tree. Idempotent."""
    global _CONFIGURED
    logger = logging.getLogger(_ROOT)
    lvl_name = (level or os.environ.get("FULCRA_LABS_LOG") or "info").upper()
    logger.setLevel(getattr(logging, lvl_name, logging.INFO))
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Component logger. ``name`` is normally ``__name__`` (already dotted
    under ``fulcra_labs``)."""
    return logging.getLogger(name)
