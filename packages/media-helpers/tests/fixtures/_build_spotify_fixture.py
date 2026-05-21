"""Build the synthetic Spotify Extended Streaming History fixture."""
import json, zipfile
from pathlib import Path

OUT = Path(__file__).parent / "spotify_extended_sample.zip"
if OUT.exists():
    OUT.unlink()

entries = [
    {"ts":"2026-05-10T20:30:00Z","platform":"OS X 14","ms_played":210000,"conn_country":"US","ip_addr":"x",
     "master_metadata_track_name":"Get Lucky","master_metadata_album_artist_name":"Daft Punk",
     "master_metadata_album_album_name":"Random Access Memories",
     "spotify_track_uri":"spotify:track:69kOkLUCkxIZYexIgSG8rq",
     "episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
     "reason_start":"clickrow","reason_end":"trackdone","shuffle":False,"skipped":False,
     "offline":False,"offline_timestamp":None,"incognito_mode":False},
    {"ts":"2026-05-10T20:33:30Z","platform":"OS X 14","ms_played":5000,"conn_country":"US","ip_addr":"x",
     "master_metadata_track_name":"Around the World","master_metadata_album_artist_name":"Daft Punk",
     "master_metadata_album_album_name":"Homework",
     "spotify_track_uri":"spotify:track:1pKYYY0dkg23sQQXi0Q5zN",
     "episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
     "reason_start":"fwdbtn","reason_end":"fwdbtn","shuffle":False,"skipped":True,
     "offline":False,"offline_timestamp":None,"incognito_mode":False},
    {"ts":"2026-05-09T18:15:00Z","platform":"iOS","ms_played":2400000,"conn_country":"US","ip_addr":"y",
     "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
     "spotify_track_uri":None,
     "episode_name":"The Crime Machine, Part I","episode_show_name":"Reply All",
     "spotify_episode_uri":"spotify:episode:abc",
     "reason_start":"clickrow","reason_end":"trackdone","shuffle":False,"skipped":False,
     "offline":False,"offline_timestamp":None,"incognito_mode":False},
    {"ts":"2026-05-09T20:00:00Z","platform":"iOS","ms_played":15000,"conn_country":"US","ip_addr":"y",
     "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
     "spotify_track_uri":None,
     "episode_name":"Skipped Episode","episode_show_name":"Reply All",
     "spotify_episode_uri":"spotify:episode:def",
     "reason_start":"fwdbtn","reason_end":"fwdbtn","shuffle":False,"skipped":False,
     "offline":False,"offline_timestamp":None,"incognito_mode":False},
    {"ts":"2026-05-09T20:30:00Z","platform":"iOS","ms_played":40000,"conn_country":"US","ip_addr":"y",
     "master_metadata_track_name":None,"master_metadata_album_artist_name":None,"master_metadata_album_album_name":None,
     "spotify_track_uri":None,"episode_name":None,"episode_show_name":None,"spotify_episode_uri":None,
     "reason_start":"unknown","reason_end":"unknown","shuffle":False,"skipped":False,
     "offline":False,"offline_timestamp":None,"incognito_mode":False},
]
with zipfile.ZipFile(OUT, "w") as zf:
    zf.writestr("Streaming_History_Audio_2026_1.json", json.dumps(entries))
print(f"wrote {OUT}")
