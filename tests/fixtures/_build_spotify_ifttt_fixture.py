"""Build the synthetic Spotify IFTTT->GDrive xlsx fixture.

Run manually: python tests/fixtures/_build_spotify_ifttt_fixture.py

The real IFTTT applets are 'Recent tracks' and 'Spotify Tracks V2', both
polling /me/player/recently-played. They produce 5-column rows with no
header:
  (timestamp, track_name, artist, spotify_track_id, spotify_url)

The two applets log near-identical data with the same timestamp for the
same play. Across applet runs the user may have several files (xlsx caps
at ~2000 rows in IFTTT's writer, so long-lived applets produce
'Recent tracks.xlsx', 'Recent tracks (1).xlsx', etc.).

The fixture has deliberate cross-applet overlap, an intra-applet replay,
and one numerified track name ('1901') that spreadsheet readers coerce
to a float — the importer must round-trip it back to a string.
"""
from pathlib import Path
from openpyxl import Workbook

OUT_DIR = Path(__file__).parent / "spotify_ifttt"
OUT_DIR.mkdir(exist_ok=True)


def _write(name: str, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in rows:
        ws.append(r)
    wb.save(OUT_DIR / name)


# Recent tracks applet — chunk 1
_write("Recent tracks.xlsx", [
    ("November 4, 2022 at 03:53PM", "Reelin' In The Years", "Steely Dan",
     "1I7zHEdDx8Ny5RxzYPqsU2", "https://open.spotify.com/track/1I7zHEdDx8Ny5RxzYPqsU2"),
    ("November 4, 2022 at 04:23PM", "Hounds of Love", "The Futureheads",
     "7mAF2MJdbNT75VrVcgwT6F", "https://open.spotify.com/track/7mAF2MJdbNT75VrVcgwT6F"),
    # Numerified track name — openpyxl reads as float
    ("October 23, 2023 at 08:10AM", 1901.0, "Phoenix",
     "1Ug5wxoHthwxctyWTUMGta", "https://open.spotify.com/track/1Ug5wxoHthwxctyWTUMGta"),
    # Replay of the SAME track later that day — must produce two distinct events
    ("October 23, 2023 at 11:45PM", "Hounds of Love", "The Futureheads",
     "7mAF2MJdbNT75VrVcgwT6F", "https://open.spotify.com/track/7mAF2MJdbNT75VrVcgwT6F"),
])

# Recent tracks applet — chunk 2 (covers later dates)
_write("Recent tracks (1).xlsx", [
    ("September 2, 2025 at 11:59AM", "Let My Love Open The Door", "Pete Townshend",
     "0otlwsD3mSogk7VJCTp6Kg", "https://open.spotify.com/track/0otlwsD3mSogk7VJCTp6Kg"),
])

# Spotify Tracks V2 applet — chunk 1.  Heavy overlap with Recent tracks chunk 1.
_write("Spotify Tracks V2.xlsx", [
    # EXACT dupe of a Recent tracks row — same (ts, track_id)
    ("November 4, 2022 at 03:53PM", "Reelin' In The Years", "Steely Dan",
     "1I7zHEdDx8Ny5RxzYPqsU2", "https://open.spotify.com/track/1I7zHEdDx8Ny5RxzYPqsU2"),
    # Same track, different time — that's a real replay, keep both
    ("October 23, 2023 at 11:45PM", "Hounds of Love", "The Futureheads",
     "7mAF2MJdbNT75VrVcgwT6F", "https://open.spotify.com/track/7mAF2MJdbNT75VrVcgwT6F"),
    # Track only this applet caught
    ("October 24, 2023 at 07:46AM", "Artefact", "Phoenix",
     "5DAkzBJ48N7z6lwY4eZ0PP", "https://open.spotify.com/track/5DAkzBJ48N7z6lwY4eZ0PP"),
])

# Spotify Tracks V2 applet — chunk 2
_write("Spotify Tracks V2 (1).xlsx", [
    ("May 16, 2026 at 08:56PM", "Reelin' In The Years", "Steely Dan",
     "1I7zHEdDx8Ny5RxzYPqsU2", "https://open.spotify.com/track/1I7zHEdDx8Ny5RxzYPqsU2"),
])

# Bundle into a zip for the importer entry point
import zipfile
zip_path = Path(__file__).parent / "spotify_ifttt_small.zip"
if zip_path.exists():
    zip_path.unlink()
with zipfile.ZipFile(zip_path, "w") as zf:
    for x in sorted(OUT_DIR.glob("*.xlsx")):
        zf.write(x, f"Spotify/{x.name}")
print(f"wrote {zip_path}")
