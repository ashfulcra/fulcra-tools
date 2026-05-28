"""Strava setup walkthrough."""

from __future__ import annotations

import click


STRAVA_STEPS = """\
Strava setup

  Strava exposes the authenticated athlete's activity feed via a direct
  REST API (`GET /api/v3/athlete/activities`). The endpoint supports
  cursor-style filtering via the `after` (unix timestamp) param, so
  incremental polls are clean.

  Setup (manual code exchange — auth-callback CLI is deferred):

  1. Create an API application:
       https://www.strava.com/settings/api
     - Application name: anything (e.g. "fulcra-media-helpers")
     - Authorization Callback Domain: localhost (any value works for
       the manual flow below — Strava just validates the *domain*).

  2. From the app page, note your client_id and client_secret.

  3. Open the authorize URL in a browser (paste your client_id):
       https://www.strava.com/oauth/authorize?client_id=<CLIENT_ID>&response_type=code&redirect_uri=http://localhost/exchange_token&approval_prompt=force&scope=read,activity:read
     Strava will prompt you to authorize the app for your account.

  4. After approving, Strava 302s to
       http://localhost/exchange_token?state=&code=<CODE>&scope=...
     The browser will say "this site can't be reached" — that's fine.
     Copy the `code=<CODE>` value out of the URL bar.

  5. Exchange the code for an access_token + refresh_token:
       curl -X POST https://www.strava.com/oauth/token \\
         -d client_id=<CLIENT_ID> \\
         -d client_secret=<CLIENT_SECRET> \\
         -d code=<CODE> \\
         -d grant_type=authorization_code
     Response:
       {
         "access_token": "...",
         "refresh_token": "...",
         "expires_at": 1700000000,
         "expires_in": 21600,
         "athlete": {"id": ..., ...}
       }

  6. Save credentials:
       mkdir -p ~/.config/fulcra-media
       cat > ~/.config/fulcra-media/strava.json <<'EOF'
       {
         "client_id": "<CLIENT_ID>",
         "client_secret": "<CLIENT_SECRET>",
         "access_token": "<ACCESS_TOKEN>",
         "refresh_token": "<REFRESH_TOKEN>",
         "expires_at": <EXPIRES_AT_UNIX_TS>
       }
       EOF
       chmod 600 ~/.config/fulcra-media/strava.json

  7. Run:
       fulcra-media import strava

  Token refresh:
  - Strava access_tokens expire every 6 hours. The importer auto-refreshes
    via the saved refresh_token (POST /oauth/token, grant_type=refresh_token)
    when expires_at is in the past — the new tokens are written back to
    strava.json in place.

  Rate limits:
  - 100 requests / 15 min, 1000 / day. First-run backfills walk all
    activities (per_page=200, sleep 100ms between pages). Pass
    --max-pages N to cap, or --since YYYY-MM-DDTHH:MM:SSZ to skip back-history.

  Advanced (out of scope for this CLI):
  - Strava supports webhook subscriptions for real-time push when activities
    are created/updated. Wiring a webhook receiver into Fulcra would replace
    polling entirely, but that path is not yet implemented here.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for setting up Strava."""
    click.echo(STRAVA_STEPS)
