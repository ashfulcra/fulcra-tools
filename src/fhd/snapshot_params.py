"""Pure builders for Daytona sandbox params. Kept side-effect-free so they are
unit-testable without touching the network."""
from __future__ import annotations

AUTO_STOP_MINUTES = 240  # 4 hours — gives guests a comfortable window to click
                         # the link without it dying under them.


def build_spawn_kwargs(
    *,
    snapshot: str,
    openrouter_model: str,
    label: str,
    skill_branch: str = "main",
) -> dict:
    """Kwargs for CreateSandboxFromSnapshotParams.

    Notes:
    - The OpenRouter *key* is deliberately NOT placed in env_vars; spawn.py writes
      it into ~/.hermes/.env instead, so a curious guest can't read it via a bare
      `echo $OPENROUTER_API_KEY` (the model id is fine to expose).
    - public=False: only the signed preview URL (which carries its own token) can
      reach the sandbox; the predictable non-signed URL will not.
    - `skill_branch` becomes FULCRA_SKILL_BRANCH in the sandbox env so start-chat.sh
      knows which ref of fulcradynamics/agent-skills to clone at boot. Defaults to
      `main`; spawn.py pins it to a pending PR branch when one is in flight so
      sandboxes pick up the PR-version skill until it merges.
    """
    return {
        "snapshot": snapshot,
        "env_vars": {
            "OPENROUTER_MODEL": openrouter_model,
            "FULCRA_SKILL_BRANCH": skill_branch,
        },
        "auto_stop_interval": AUTO_STOP_MINUTES,
        "public": False,
        "labels": {"fhd": "guest", "guest": label},
    }
