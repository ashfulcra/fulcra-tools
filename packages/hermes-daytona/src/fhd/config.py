"""Load and validate operator credentials/config from the environment.

WHY: every script needs the same creds (Daytona + OpenRouter). Centralizing
load+validation here means a missing key fails fast with a clear message
instead of an opaque SDK auth error three calls deep.
"""
from __future__ import annotations
import os
from dataclasses import dataclass

DEFAULT_API_URL = "https://app.daytona.io/api"
DEFAULT_TARGET = "us"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

@dataclass(frozen=True)
class Settings:
    daytona_api_key: str
    openrouter_api_key: str
    openrouter_model: str
    daytona_api_url: str
    daytona_target: str

def load_settings(use_dotenv: bool = True) -> Settings:
    if use_dotenv:
        from dotenv import load_dotenv
        load_dotenv()
    missing = [k for k in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY") if not os.environ.get(k)]
    if missing:
        raise ValueError(f"Missing required env var(s): {', '.join(missing)} (set them in .env)")
    return Settings(
        daytona_api_key=os.environ["DAYTONA_API_KEY"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        daytona_api_url=os.environ.get("DAYTONA_API_URL", DEFAULT_API_URL),
        daytona_target=os.environ.get("DAYTONA_TARGET", DEFAULT_TARGET),
    )
