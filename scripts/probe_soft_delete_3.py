"""Third pass: explore the 405-returning paths to find allowed methods."""
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
    return subprocess.run(
        ["uv", "run", "fulcra", "auth", "print-access-token"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def main() -> int:
    token = get_token()
    c = httpx.Client(
        base_url=BASE, timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
    )

    # Set up another throwaway event so we can confirm any successful delete
    body = {
        "annotation_type": "duration",
        "name": "Soft Delete Probe 3",
        "description": "Throwaway.",
        "tags": [],
        "measurement_spec": {"measurement_type": "duration", "value_type": "duration", "unit": None},
    }
    def_id = c.post("/user/v1alpha1/annotation", json=body).json()["id"]
    def_source = f"com.fulcradynamics.annotation.{def_id}"

    start = datetime.now(timezone.utc) - timedelta(days=365 * 8 + 2)
    end = start + timedelta(seconds=10)
    source_id = f"com.fulcra.media.probe3.{int(time.time())}"
    record = {
        "specversion": 1,
        "data": json.dumps({"note": "probe 3 event"}, sort_keys=True),
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
    c.post("/ingest/v1/record/batch",
           content=json.dumps(record).encode(),
           headers={"content-type": "application/x-jsonl"})
    time.sleep(1)

    qs = (start - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    qe = (end + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    records = c.get("/data/v1alpha1/event/DurationAnnotation",
                    params={"start_time": qs, "end_time": qe}).json()
    matching = [rec for rec in records if source_id in (rec.get("sources") or [])]
    event_id = matching[0]["id"]
    print(f"event_id: {event_id}, source_id: {source_id}")

    # The 405-returning paths
    paths = [
        "/data/v1alpha1/event/DurationAnnotation",
        "/data/v1alpha1/event/delete",
    ]
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    print()
    print("=== method matrix ===")
    for path in paths:
        print(f"\n--- {path}")
        for method in methods:
            req = httpx.Request(method, BASE + path,
                                headers={"Authorization": f"Bearer {token}"})
            try:
                r = c.send(req)
            except Exception as e:
                print(f"  {method:8} -> EXC {e}")
                continue
            allow = r.headers.get("allow", "")
            print(f"  {method:8} -> {r.status_code}  allow={allow!r}  body={r.text[:80]!r}")

    # Try yet more shapes informed by what came back
    print()
    print("=== more shape attempts ===")
    extra = [
        # Maybe the data endpoint is read-only and the ingest endpoint
        # supports DELETE on the batch path
        ("DELETE", "/ingest/v1/record/batch", None),
        ("PUT",    "/ingest/v1/record/batch",
            json.dumps({"specversion": 1, "data": json.dumps({"note":"x"}),
                        "metadata": {"deleted_at":"now", **record["metadata"]}}).encode()),
        # Maybe there's an annotation-write endpoint we haven't tried
        ("POST",   "/user/v1alpha1/annotation/event", record),
        ("DELETE", "/user/v1alpha1/event", None),
        # The /data/v1alpha1/event/delete path is real; try DELETE method
        ("DELETE", "/data/v1alpha1/event/delete", None),
        ("PUT",    "/data/v1alpha1/event/delete", {"ids": [event_id], "sources": [source_id]}),
    ]
    for method, path, payload in extra:
        if method == "DELETE":
            r = c.delete(path)
        elif method == "PUT":
            if isinstance(payload, (bytes, bytearray)):
                r = c.put(path, content=payload,
                          headers={"content-type": "application/x-jsonl"})
            else:
                r = c.put(path, json=payload or {})
        elif method == "POST":
            r = c.post(path, json=payload or {})
        else:
            continue
        if r.status_code not in (404, 405):
            print(f"  *** {method} {path} -> {r.status_code} body={r.text[:200]!r}")
        else:
            print(f"  {method:6} {path[:60]:60} -> {r.status_code} allow={r.headers.get('allow', '')!r}")

    # Final check — is the event still there?
    time.sleep(2)
    records = c.get("/data/v1alpha1/event/DurationAnnotation",
                    params={"start_time": qs, "end_time": qe}).json()
    still = [rec for rec in records if source_id in (rec.get("sources") or [])]
    print(f"\nevent still present: {bool(still)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
