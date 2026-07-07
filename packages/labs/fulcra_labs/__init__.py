"""fulcra-labs — one canonical Fulcra data track per lab marker.

Observations arrive by parsing lab-report PDFs (any provider). The AGENT does
the PDF extraction (guided by the fulcra-lab-results SKILL); this package does
everything deterministic: marker normalization, unit conversion, validation,
idempotent storage. Accuracy posture is VERIFY-BEFORE-INGEST — the inverse of
the media plugins' over-capture — because this is medical data.
"""
from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
