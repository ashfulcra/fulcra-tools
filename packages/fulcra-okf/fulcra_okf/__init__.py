"""fulcra-okf: canonical Python library for Open Knowledge Format (OKF) v0.1."""
from __future__ import annotations

from .spec import OKF_VERSION


class OKFError(Exception):
    """Base class for all fulcra-okf errors."""


# Re-exports (imported after OKFError to avoid a circular import in frontmatter).
from .frontmatter import FrontmatterError  # noqa: E402
from .concept import Concept  # noqa: E402
from .bundle import Bundle  # noqa: E402
from .validate import validate, Report, Finding  # noqa: E402
from . import ext  # noqa: E402

__all__ = [
    "OKF_VERSION", "OKFError", "FrontmatterError",
    "Concept", "Bundle", "validate", "Report", "Finding", "ext",
]
