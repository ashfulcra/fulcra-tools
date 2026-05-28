"""Pure builders for Daytona sandbox params. Kept side-effect-free so they are
unit-testable without touching the network."""
from __future__ import annotations

AUTO_STOP_MINUTES = 30


def build_spawn_kwargs(*, snapshot: str, openrouter_model: str, label: str) -> dict:
    """Kwargs for CreateSandboxFromSnapshotParams.

    Notes:
    - The OpenRouter *key* is deliberately NOT placed in env_vars; spawn.py writes
      it into ~/.hermes/.env instead, so a curious guest can't read it via a bare
      `echo $OPENROUTER_API_KEY` (the model id is fine to expose).
    - public=False: only the signed preview URL (which carries its own token) can
      reach the sandbox; the predictable non-signed URL will not.
    """
    return {
        "snapshot": snapshot,
        "env_vars": {"OPENROUTER_MODEL": openrouter_model},
        "auto_stop_interval": AUTO_STOP_MINUTES,
        "public": False,
        "labels": {"fhd": "guest", "guest": label},
    }
