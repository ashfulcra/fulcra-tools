"""Spawn ONE guest sandbox and print a signed, clickable preview URL.

The guest opens the URL and lands in the locked-down Hermes chat (dashboard
fronted by Caddy). The OpenRouter key is written into the sandbox's
~/.hermes/.env (never baked into the image, never put in sandbox env_vars).
"""
from __future__ import annotations
import argparse

from daytona import (
    Daytona,
    DaytonaConfig,
    CreateSandboxFromSnapshotParams,
    SessionExecuteRequest,
)

from fhd.config import load_settings
from fhd.snapshot_params import build_spawn_kwargs

DASHBOARD_PORT = 8080
PREVIEW_TTL_SECONDS = 3600  # link valid 1h; sandbox auto-stops after 30m idle anyway

# Until the upstream fulcradynamics/agent-skills PR merges, pin sandboxes to the
# PR branch so they get the fixed skill (silent pre-flight + reliable
# device-flow auth). Once the PR merges, change this back to "main" (the boot
# script's overrides are self-disabling, so either value is safe — main once
# merged just removes one needless network hop).
SKILL_BRANCH = "fix/preconfigured-env-and-reliable-auth"


def main() -> None:
    ap = argparse.ArgumentParser(description="Spawn a guest Hermes sandbox")
    ap.add_argument("label", help="A label for this guest, e.g. 'alice'")
    ap.add_argument("--snapshot", default="fhd-hermes-demo")
    ap.add_argument(
        "--skill-branch",
        default=SKILL_BRANCH,
        help="Branch of fulcradynamics/agent-skills to fetch at boot (default: %(default)s)",
    )
    args = ap.parse_args()

    s = load_settings()
    d = Daytona(
        DaytonaConfig(
            api_key=s.daytona_api_key,
            api_url=s.daytona_api_url,
            target=s.daytona_target,
        )
    )

    kwargs = build_spawn_kwargs(
        snapshot=args.snapshot,
        openrouter_model=s.openrouter_model,
        label=args.label,
        skill_branch=args.skill_branch,
    )
    print(f"Spawning sandbox for guest '{args.label}' ...")
    sb = d.create(CreateSandboxFromSnapshotParams(**kwargs), timeout=300)

    # Inject the OpenRouter key into ~/.hermes/.env (passed via env= so it never
    # appears in the command string / process list).
    sb.process.exec(
        "bash -lc 'mkdir -p ~/.hermes && "
        'printf "OPENROUTER_API_KEY=%s\\n" "$OPENROUTER_API_KEY" >> ~/.hermes/.env\'',
        env={"OPENROUTER_API_KEY": s.openrouter_api_key},
    )

    # Launch the guest-facing chat (dashboard + Caddy) as a long-running session.
    sb.process.create_session("chat")
    sb.process.execute_session_command(
        "chat",
        SessionExecuteRequest(command="bash -lc '/opt/fhd/start-chat.sh'", run_async=True),
    )

    preview = sb.create_signed_preview_url(DASHBOARD_PORT, PREVIEW_TTL_SECONDS)
    print(f"\nGuest '{args.label}' is ready.")
    print(f"  sandbox id: {sb.id}")
    print(f"  PRESS PLAY (send this link): {preview.url}")
    print("  (The chat takes ~15-20s to come up on first load while the UI builds.)")


if __name__ == "__main__":
    main()
