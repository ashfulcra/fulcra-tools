"""Deezer setup walkthrough."""

from __future__ import annotations

import click


DEEZER_STEPS = """\
Deezer setup

  Deezer exposes the authenticated user's full play history via a real,
  documented REST endpoint — `GET /user/me/history`. Unlike Spotify's
  recently-played API (50-track ceiling), Deezer paginates indefinitely
  via a `next` URL. Cleanest non-Last.fm direct API path for music.

  Setup (manual token mint — auth-code-flow CLI is deferred):
  1. Sign in to https://developers.deezer.com and register an app.
  2. Visit the OAuth section: https://developers.deezer.com/api/oauth
     - Redirect URI: anything you control (or paste `https://localhost/`
       and you'll grab the token off the redirect URL manually).
     - Required permissions: `listening_history` (read play history)
  3. Construct the authorize URL:
       https://connect.deezer.com/oauth/auth.php?app_id=<APP_ID>&redirect_uri=<URI>&perms=listening_history
  4. Approve in browser. Deezer 302s to your redirect URL with `?code=<CODE>`.
  5. Exchange the code for an access token:
       curl -sG https://connect.deezer.com/oauth/access_token.php \\
         --data-urlencode "app_id=<APP_ID>" \\
         --data-urlencode "secret=<APP_SECRET>" \\
         --data-urlencode "code=<CODE>" \\
         --data-urlencode "output=json"
     Response: {"access_token": "<TOKEN>", "expires": 0}
     (Deezer access tokens for personal apps generally don't expire.)
  6. Save credentials:
       mkdir -p ~/.config/fulcra-media
       cat > ~/.config/fulcra-media/deezer.json <<'EOF'
       {"access_token": "<TOKEN>"}
       EOF
       chmod 600 ~/.config/fulcra-media/deezer.json

  7. Run:
       fulcra-media import deezer

  Caveats:
  - The token is single-account: it reads YOUR Deezer history.
  - Rate limit is 50 requests / 5 seconds. The importer sleeps 100ms
    between page fetches.
  - First run pulls full history; use --max-pages N to cap, or
    --since YYYY-MM-DDTHH:MM:SSZ to start at a date.
  - Subsequent runs are incremental — watermark stored in state.json.
    The poller fetches from (watermark - 1 hour) to catch any late
    server-side reordering. Source-id dedup handles the overlap.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(DEEZER_STEPS)
