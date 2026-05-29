"""Declarative Daytona image for the demo sandbox.

Bakes uv + the Fulcra CLI + Hermes (OpenRouter, no Playwright) + Caddy, prebuilds
the Hermes dashboard web bundle, copies in the fulcra-onboarding skill and our
asset files, and pre-sets the OpenRouter provider/model. Secrets are NOT baked —
only injected at spawn. Built server-side by Daytona (no local Docker needed).

Every command here was validated in the Task 1 spike. See
docs/superpowers/findings/2026-05-28-spike-findings.md.
"""
from __future__ import annotations
from pathlib import Path
from daytona import Image

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS = REPO_ROOT / "assets"
HERMES_HOME = "/root/.hermes"
# Node lives in /root/.hermes/node/bin — required on PATH so the dashboard can
# build its web UI at first launch (otherwise: "npm is not available").
PATH_ENV = "/root/.local/bin:/root/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"

HERMES_INSTALL = (
    "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh "
    "| bash -s -- --skip-browser --skip-setup"
)
SKILL_CLONE = (
    "git clone --depth 1 https://github.com/fulcradynamics/agent-skills /tmp/agent-skills "
    "&& mkdir -p /root/.hermes/skills/fulcra "
    "&& cp -r /tmp/agent-skills/skills/fulcra-onboarding /root/.hermes/skills/fulcra/fulcra-onboarding "
    "&& rm -rf /tmp/agent-skills"
)


def build_image_commands() -> list[str]:
    """The shell commands baked into the image, in order. Separated out so it is
    unit-testable without a network call."""
    return [
        # System deps. git/procps/build deps are required by the Hermes installer.
        "apt-get update && apt-get install -y curl ca-certificates git build-essential python3-dev libffi-dev procps",
        # uv (installs to /root/.local/bin) + the Fulcra CLI.
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "uv tool install fulcra-api",
        # Hermes agent (no Playwright, no setup wizard).
        HERMES_INSTALL,
        # Caddy static binary for the locked-down reverse proxy.
        "curl -L -o /usr/local/bin/caddy 'https://caddyserver.com/api/download?os=linux&arch=amd64' && chmod +x /usr/local/bin/caddy && caddy version",
        # Onboarding skill via file copy (Hermes's installer scanner blocks `hermes skills install`).
        SKILL_CLONE,
        # Pre-set OpenRouter provider + model (the API key is injected at spawn, not here).
        "hermes config set model.provider openrouter && hermes config set model.default anthropic/claude-sonnet-4.5",
        # NOTE: the dangerous-command approval prompt is bypassed at runtime via the
        # HERMES_YOLO_MODE=1 env var exported in start-chat.sh — NOT via a config key.
        # (The config `approvals.mode` setting is not wired to the approval logic.)
        # NOTE: we do NOT prebuild the dashboard web bundle here. `npm run build`
        # needs dev deps (tsc) the pip install doesn't provide; `hermes dashboard`
        # builds the bundle itself on first launch (~15s, covered by start-chat.sh's
        # health-poll). So start-chat.sh deliberately does NOT pass --skip-build.
        # Staging dir for our assets.
        "mkdir -p /opt/fhd",
    ]


def build_image() -> Image:
    img = Image.debian_slim("3.12").env({"PATH": PATH_ENV, "HERMES_HOME": HERMES_HOME})
    for cmd in build_image_commands():
        img = img.run_commands(cmd)
    # Copy assets into place (run_commands above already created HERMES_HOME via the
    # Hermes install and /opt/fhd via mkdir, so these destinations exist).
    img = (
        img.add_local_file(str(ASSETS / "hermes" / "SOUL.md"), f"{HERMES_HOME}/SOUL.md")
        .add_local_file(str(ASSETS / "hermes" / "AGENTS.md"), f"{HERMES_HOME}/AGENTS.md")
        .add_local_file(str(ASSETS / "caddy" / "Caddyfile"), "/opt/fhd/Caddyfile")
        .add_local_file(str(ASSETS / "hermes" / "start-chat.sh"), "/opt/fhd/start-chat.sh")
    )
    img = img.run_commands("chmod +x /opt/fhd/start-chat.sh")
    return img
