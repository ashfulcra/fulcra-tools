"""Second pass: try bulk / source-id-based delete shapes."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")


def get_token() -> str:
    env = os.environ.get("FULCRA_ACCESS_TOKEN")
    if env:
        return env
    result = subprocess.run(
        ["uv", "run", "fulcra", "auth", "print-access-token"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def main() -> int:
    token = get_token()
    c = httpx.Client(
        base_url=BASE, timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
    )

    # New throwaway def + event so we can experiment cleanly
    body = {
        "annotation_type": "duration",
        "name": "Soft Delete Probe 2",
        "description": "Throwaway. probe_soft_delete_2.py.",
        "tags": [],
        "measurement_spec": {"measurement_type": "duration", "value_type": "duration", "unit": None},
    }
    def_id = c.post("/user/v1alpha1/annotation", json=body).json()["id"]
    def_source = f"com.fulcradynamics.annotation.{def_id}"
    print(f"def_id: {def_id}")

    start = datetime.now(timezone.utc) - timedelta(days=365 * 8 + 1)
    end = start + timedelta(seconds=10)
    source_id = f"com.fulcra.media.probe2.{int(time.time())}"
    record = {
        "specversion": 1,
        "data": json.dumps({"note": "probe 2 event"}, sort_keys=True),
        "metadata": {
            "data_type": "DurationAnnotation",
            "recorded_at": {
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
            "tags": [],
            "source": [source_id, def_source],
            "content_type": "application/json",
        },
    }
    r = c.post("/ingest/v1/record/batch",
               content=json.dumps(record, sort_keys=True).encode(),
               headers={"content-type": "application/x-jsonl"})
    print(f"ingest -> {r.status_code}")
    time.sleep(1)

    qs = (start - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    qe = (end + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    records = c.get("/data/v1alpha1/event/DurationAnnotation",
                    params={"start_time": qs, "end_time": qe}).json()
    matching = [rec for rec in records if source_id in (rec.get("sources") or [])]
    if not matching:
        print("event not found, aborting")
        return 1
    event_id = matching[0]["id"]
    print(f"event_id: {event_id}")

    print()
    print("=== probe more endpoint shapes ===")
    probes = [
        # Unversioned / different prefixes
        ("DELETE", f"/event/DurationAnnotation/{event_id}", None),
        ("DELETE", f"/event/{event_id}", None),
        # By source id (URL-encoded)
        ("DELETE", f"/data/v1alpha1/event/DurationAnnotation?source={source_id}", None),
        ("DELETE", f"/data/v1alpha1/event/DurationAnnotation?source_id={source_id}", None),
        # Bulk delete endpoints
        ("POST",   "/data/v1alpha1/event/DurationAnnotation/delete", {"sources": [source_id]}),
        ("POST",   "/data/v1alpha1/event/delete", {"ids": [event_id]}),
        ("POST",   "/user/v1alpha1/event/delete", {"ids": [event_id]}),
        ("POST",   "/ingest/v1/record/delete", {"sources": [source_id]}),
        ("POST",   "/ingest/v1/delete", {"sources": [source_id]}),
        # Versioned alts
        ("DELETE", f"/data/v1/event/DurationAnnotation/{event_id}", None),
        ("DELETE", f"/data/v1beta1/event/DurationAnnotation/{event_id}", None),
        # Annotation-scoped event delete
        ("DELETE", f"/user/v1alpha1/annotation/{def_id}/events?source={source_id}", None),
    ]
    for method, path, body in probes:
        if method == "DELETE":
            r = c.delete(path)
        else:
            r = c.post(path, json=body or {})
        if r.status_code != 404:
            print(f"  *** {method:6} {path}")
            print(f"     -> {r.status_code} {r.text[:200]!r}")
        else:
            print(f"  {method:6} {path[:80]:80} -> 404")

    print()
    # Did any of them actually hide the event?
    time.sleep(2)
    records = c.get("/data/v1alpha1/event/DurationAnnotation",
                    params={"start_time": qs, "end_time": qe}).json()
    still = [rec for rec in records if source_id in (rec.get("sources") or [])]
    print(f"event still present after all probes: {bool(still)}")
    return 0


import sys as _sys
if "--i-know-this-hits-prod" not in _sys.argv:
    print("Refusing to run: this script creates real annotation defs against",
          file=_sys.stderr)
    print(f"  {os.environ.get('FULCRA_API_BASE', 'https://api.fulcradynamics.com')}",
          file=_sys.stderr)
    print("Pass --i-know-this-hits-prod to proceed.", file=_sys.stderr)
    _sys.exit(2)
_sys.argv.remove("--i-know-this-hits-prod")

if __name__ == "__main__":
    raise SystemExit(main())
