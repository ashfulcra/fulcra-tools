# Getting a viewing-history export out of Netflix

Two routes exist. The **slim CSV** is instant and is the demo path — use it for
first-time onboarding. The **GDPR export** is the upgrade: real timestamps,
real durations, every profile, full account lifetime — but Netflix takes days
to deliver it, so never make a new user wait on it.

> **Provenance:** step wording adapted from the `fulcra-media` Netflix wizard
> (`packages/media-helpers/fulcra_media/wizards/netflix.py` in
> [ashfulcra/fulcra-tools](https://github.com/ashfulcra/fulcra-tools), MIT).

## Route 1 — slim CSV (in-app, instant)

Reference: <https://help.netflix.com/en/node/101917>

Walk the user through:

1. Open <https://www.netflix.com/account> in a web browser.
2. Select **Profiles**, then choose the profile whose history you want.
3. Open **Viewing activity**.
4. Click **Show More** at the bottom until all entries are loaded — long
   histories take several clicks; the download only contains what's been
   loaded onto the page.
5. Click **Download all**.
6. Save the file (filename is usually `NetflixViewingHistory.csv`) and send it
   back in chat, or provide its path if the agent runs on the same machine.

What the file is: exactly two columns, `Title,Date`, with dates in `M/D/YY`
format and **no** time of day, duration, device, or profile fields. One
profile per download — repeat per profile if the user wants more than one
(each import is idempotent, so importing several profiles' files in sequence
is fine).

How the importer treats it: each row becomes one Watched annotation as a
point-in-time event at **12:00 UTC on the date** (a synthetic time — the
export has none) with a 1-second duration, marked `timestamp_confidence:
"low"`, `point_in_time: true`. Two identical `Title,Date` rows are treated as
a genuine same-day rewatch, not a duplicate.

## Route 2 — GDPR full export ("Download your personal information")

Reference: <https://help.netflix.com/en/node/100624>

1. Open <https://www.netflix.com/account/getmyinfo> in a web browser.
2. Follow Netflix's verification prompts (email confirmation + re-auth).
3. Submit the request. Netflix says delivery may take up to 30 days; in
   practice it's usually 1–5 days.
4. When the email arrives, download the ZIP promptly — **the download link is
   only valid for 7 days**.
5. The relevant file inside the ZIP is
   `CONTENT_INTERACTION/ViewingActivity.csv` — the 10-column rich variant
   (Profile Name, Start Time in UTC, Duration as H:MM:SS, Title, Supplemental
   Video Type, Device Type, and more).

How the importer treats it: each row becomes one Watched annotation with the
**real UTC start time and real duration** — `timestamp_confidence: "high"`,
no estimates. Trailer/hook/promotional rows (non-empty Supplemental Video
Type) are filtered out automatically; they're autoplay previews, not viewing.
Scope covers **all profiles** in the account and the full account lifetime.

The importer auto-detects which variant it's been given from the CSV header —
no flag needed, same command either way.

## Precision trade-off at a glance

| | Slim CSV | GDPR export |
|---|---|---|
| Availability | Instant, in-app | 1–5+ days after request (Netflix quotes up to 30) |
| Download-link validity | n/a (direct download) | 7 days |
| Timestamps | Date only; synthetic 12:00 UTC | Real UTC start times |
| Durations | None; synthetic 1 s | Real, H:MM:SS |
| Confidence marking | `low`, `point_in_time: true` | `high` |
| Profiles | One per download | All profiles in the account |
| History depth | What's loaded on the Viewing-activity page | Full account lifetime |
| Junk filtering | n/a (page shows real views) | Trailers/previews auto-dropped |

## Upgrading later

A user who onboarded with the slim CSV can request the GDPR export at any time
and re-run the import with `ViewingActivity.csv` when it arrives. The two
variants use **different deterministic ID schemes** (see
[record-schema.md](record-schema.md)), so GDPR records land alongside the slim
ones rather than replacing them — the slim records remain, marked low
confidence, and the GDPR records carry the trustworthy timestamps. Downstream
consumers that care can prefer high-confidence records; matching between the
two is what the shared `content_fingerprint` field is for.
