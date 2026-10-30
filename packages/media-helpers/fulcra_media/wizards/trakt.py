"""Trakt setup walkthrough."""

from __future__ import annotations

import click


TRAKT_STEPS = """\
Trakt API setup
  Reference: https://developer.trakt.tv/docs/authentication-oauth

  Trakt aggregates watch history across many streaming services. Once
  connected, it backfills history from supported services (Apple TV+,
  Netflix, Disney+, Hulu, Prime Video, Max, Paramount+) automatically.

  1. If you don't have an account, sign up at https://trakt.tv.
  2. Trakt VIP enables the 'Streaming Scrobbler' which syncs streaming-
     service watches in real time. Highly recommended for ongoing capture.
  3. API apps now live at https://developer.trakt.tv/apps.
     The developer portal requires a verified GitHub account. New PKCE
     sign-in needs a Client ID only, not a Client Secret. Existing apps
     may still be available at https://trakt.tv/oauth/applications/new.
     Manage account authorizations separately at:
       https://app.trakt.tv/settings/apps/connected

  4. Use Fulcra Collect's Trakt setup wizard for browser sign-in.
     For a new personal app, choose App type: Development, which accepts
     local HTTP callbacks. Production apps require HTTPS. Register the
     exact callback shown by Collect (including host and port).
     Keep an existing working app and its credentials.

  5. Existing CLI setups keep using ~/.config/fulcra-media/trakt.json
     with their previously issued OAuth tokens. The CLI does not have
     an auth subcommand; Collect credentials are stored separately.
     Once CLI credentials are already configured, import:
       fulcra-media import trakt

  Known quirk: when you connect a streaming service to Trakt, Trakt may
  backfill that service's history with the connection-day timestamp
  rather than the real watch time. The importer detects clusters
  (>=5 items sharing watched_at) and flags them timestamp_confidence: low
  so consumers can filter or weight accordingly.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for setting up Trakt."""
    click.echo(TRAKT_STEPS)
