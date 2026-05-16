"""Netflix wizard: walks the user through requesting and importing their data.

Two routes:
  1. Slim CSV (in-app per-profile, two columns Title/Date)
  2. GDPR full export (10-column, takes up to 30 days)

The wizard prints the canonical steps and links to Netflix's help pages.
Upload-and-import integration is added in a follow-up step.
"""

from __future__ import annotations

import click


SLIM_STEPS = """\
Netflix slim CSV (in-app per-profile download)
  Reference: https://help.netflix.com/en/node/101917

  1. Open https://www.netflix.com/account in a web browser.
  2. Select 'Profiles', then choose the profile whose history you want.
  3. Open 'Viewing activity'.
  4. Click 'Show More' at the bottom until all entries are loaded.
  5. Click 'Download all'.
  6. Save the file (filename usually NetflixViewingHistory.csv).

  Note: The slim CSV is date-only (M/D/YY format) with no time, duration,
  device, or profile fields. Each row becomes one Watched annotation with a
  synthetic 21:00 UTC start time and a duration estimated by title shape
  (movie ~ 100 min, episode ~ 30 min). timestamp_confidence: low.

  When the file is ready, run:
    fulcra-media import netflix /path/to/NetflixViewingHistory.csv
"""

GDPR_STEPS = """\
Netflix GDPR / "Request your personal information" export (RECOMMENDED)
  Reference: https://help.netflix.com/en/node/100624

  1. Open https://www.netflix.com/account/getmyinfo in a web browser.
  2. Follow Netflix's verification prompts (email confirmation + re-auth).
  3. Submit the request. Netflix says delivery may take up to 30 days
     (in practice usually 1-5 days).
  4. When you receive the email link, download the ZIP. The download link is
     valid for 7 days.
  5. Inside the ZIP, the relevant file is:
       CONTENT_INTERACTION/ViewingActivity.csv
     This is the 10-column rich variant (Profile Name, Start Time UTC,
     Duration H:MM:SS, Title, Supplemental Video Type, Device Type, ...).

  Scope: covers ALL profiles in your account and your full account lifetime.

  Importing the rich variant is not yet wired up (the slim importer is in
  place). For now, upload the ZIP to your Fulcra Library and we'll wire the
  rich importer in the next milestone.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for requesting a Netflix viewing export."""
    click.echo("Which Netflix export do you want to set up?")
    click.echo("  1. Slim CSV (in-app, instant download, date-only precision)")
    click.echo("  2. GDPR full export (richer schema, takes up to 30 days) [RECOMMENDED]")
    choice = click.prompt(
        "Choose 1 or 2",
        type=click.Choice(["1", "2"]),
        show_choices=False,
    )
    click.echo("")
    click.echo(SLIM_STEPS if choice == "1" else GDPR_STEPS)
