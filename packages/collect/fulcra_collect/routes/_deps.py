"""Shared dependencies + Pydantic bodies + helpers for the route modules.

The route modules each take a :class:`RouteContext` so they can reach
the daemon and the common auth / Fulcra-client helpers without
re-implementing the same closures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx
    from ..daemon import Daemon


# ---------------------------------------------------------------------------
# Pydantic request bodies (moved verbatim from web.py)
# ---------------------------------------------------------------------------

class SecretBody(BaseModel):
    secret: str


class FulcraTokenBody(BaseModel):
    token: str


class DefinitionBindBody(BaseModel):
    definition_id: str | None = None
    force_new: bool = False
    # Optional custom name for the "Create new" path — used verbatim
    # by the resolver (no machine-id suffix). Empty/whitespace = use
    # the plugin's canonical_definition_name + suffix as before.
    new_name: str | None = None


class RecordAnnotationBody(BaseModel):
    definition_id: str
    comment: str | None = None
    # Optional Duration record window — both must be set together. ISO-8601
    # UTC strings (trailing 'Z' or '+00:00' accepted). When unset, the
    # daemon writes a Moment at now (the original Sprint A behavior).
    start_time: str | None = None
    end_time: str | None = None


class QuickRecordFavoritesBody(BaseModel):
    """Replace-all body for PUT /api/quick-record/favorites — the caller
    sends the full desired set of favorite def_ids each time. Simpler
    than separate add/remove endpoints (the UI already knows the full
    list) and lets the daemon write the file in one atomic step."""
    favorites: list[str]


# ---------------------------------------------------------------------------
# Shared context handed to every route module's register() function.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteContext:
    """Container for the shared state every route module needs.

    Carrying these via a dataclass lets each ``register(app, ctx)`` function
    use the same names without re-creating closures over ``build_app``'s
    locals.
    """
    daemon: "Daemon"
    require_token: Callable[..., None]
    require_plugin: Callable[[str], None]
    fulcra_token_or_401: Callable[[], str]
    fulcra_http_client: Callable[[str], "httpx.Client"]
