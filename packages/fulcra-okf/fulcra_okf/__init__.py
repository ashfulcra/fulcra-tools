"""fulcra-okf: canonical Python library for Open Knowledge Format (OKF) v0.1."""
from __future__ import annotations

from .spec import OKF_VERSION


class OKFError(Exception):
    """Base class for all fulcra-okf errors."""


__all__ = ["OKF_VERSION", "OKFError"]
