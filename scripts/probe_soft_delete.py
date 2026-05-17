"""Probe the Fulcra API to discover the event soft-delete mechanism.

Approach: create a throwaway annotation def, post a single test event,
then try a series of DELETE / PATCH / re-POST shapes. Inspect responses
and subsequent GETs to determine which mechanism (if any) actually
removes the event from queries.

This script touches ONLY a freshly-created annotation def named
'Soft Delete Probe' — never Watched/Listened/anything pre-existing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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
    client = httpx.Client(
        base_url=BASE, timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
    )

    print("=== STEP 1: create throwaway annotation def ===")
    body = {
        "annotation_type": "duration",
        "name": "Soft Delete Probe",
        "description": "Throwaway. Created by probe_soft_delete.py.",
        "tags": [],
        "measurement_spec": {
            "measurement_type": "duration",
            "value_type": "duration",
            "unit": None,
        },
    }
    r = client.post("/user/v1alpha1/annotation", json=body)
    print(f"  POST annotation -> {r.status_code}")
    if r.status_code >= 400:
        print(f"  body: {r.text}")
        return 1
    def_id = r.json()["id"]
    def_source = f"com.fulcradynamics.annotation.{def_id}"
    print(f"  def_id: {def_id}")

    print()
    print("=== STEP 2: post a single test event ===")
    start = datetime.now(timezone.utc) - timedelta(days=365 * 8)  # far in the past
    end = start + timedelta(seconds=10)
    source_id = f"com.fulcra.media.probe.{int(time.time())}"
    record = {
        "specversion": 1,
        "data": json.dumps({"note": "probe event"}, sort_keys=True),
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
    r = client.post(
        "/ingest/v1/record/batch",
        content=json.dumps(record, sort_keys=True).encode(),
        headers={"content-type": "application/x-jsonl"},
    )
    print(f"  POST ingest -> {r.status_code}")
    if r.status_code >= 400:
        print(f"  body: {r.text}")

    # Pause briefly so the index catches up before we query
    time.sleep(2)

    print()
    print("=== STEP 3: verify event appears in query ===")
    query_start = (start - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    query_end = (end + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    r = client.get(
        "/data/v1alpha1/event/DurationAnnotation",
        params={"start_time": query_start, "end_time": query_end},
    )
    print(f"  GET event -> {r.status_code}, records: {len(r.json())}")
    matching = [rec for rec in r.json() if source_id in (rec.get("sources") or [])]
    if not matching:
        print(f"  ERROR: posted event not found. sample: {r.json()[:1]}")
        return 2
    event_record = matching[0]
    event_id = event_record["id"]
    print(f"  event id: {event_id}")
    print(f"  full record keys: {sorted(event_record.keys())}")

    print()
    print("=== STEP 4: probe possible soft-delete endpoints ===")

    candidates = [
        ("DELETE", f"/data/v1alpha1/event/DurationAnnotation/{event_id}", None),
        ("DELETE", f"/user/v1alpha1/event/{event_id}", None),
        ("DELETE", f"/user/v1alpha1/annotation/{def_id}/event/{event_id}", None),
        ("PATCH",  f"/data/v1alpha1/event/DurationAnnotation/{event_id}",
            {"deleted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
        ("POST",   f"/data/v1alpha1/event/DurationAnnotation/{event_id}/delete", None),
        ("POST",   f"/user/v1alpha1/event/{event_id}/delete", None),
        # The 'tombstone' variant: re-POST same source-id with deleted flag
        ("POST-tombstone", "/ingest/v1/record/batch", None),
    ]

    findings: list[dict] = []
    for method, path, payload in candidates:
        if method == "POST-tombstone":
            tomb = dict(record)
            tomb_data = json.loads(record["data"])
            tomb_data["deleted"] = True
            tomb["data"] = json.dumps(tomb_data, sort_keys=True)
            tomb["metadata"] = dict(record["metadata"])
            tomb["metadata"]["deleted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            r = client.post(
                path,
                content=json.dumps(tomb, sort_keys=True).encode(),
                headers={"content-type": "application/x-jsonl"},
            )
        elif method == "DELETE":
            r = client.delete(path)
        elif method == "PATCH":
            r = client.patch(path, json=payload or {})
        elif method == "POST":
            r = client.post(path, json=payload or {})
        else:
            continue
        print(f"  {method:18} {path}")
        print(f"     -> {r.status_code} (len={len(r.content)})")
        body_preview = r.text[:200]
        print(f"     body: {body_preview!r}")
        findings.append({
            "method": method,
            "path": path,
            "status": r.status_code,
            "body_preview": body_preview,
        })

    time.sleep(2)

    print()
    print("=== STEP 5: re-query — did anything actually remove the event? ===")
    r = client.get(
        "/data/v1alpha1/event/DurationAnnotation",
        params={"start_time": query_start, "end_time": query_end},
    )
    still_there = [rec for rec in r.json() if source_id in (rec.get("sources") or [])]
    print(f"  event still present: {bool(still_there)}")
    if still_there:
        print(f"  record now: {json.dumps(still_there[0], indent=2, default=str)[:500]}")

    print()
    print("=== STEP 6: try soft-deleting the WHOLE annotation def ===")
    r = client.delete(f"/user/v1alpha1/annotation/{def_id}")
    print(f"  DELETE annotation -> {r.status_code}")
    print(f"  body: {r.text[:200]!r}")

    time.sleep(2)
    r = client.get(
        "/data/v1alpha1/event/DurationAnnotation",
        params={"start_time": query_start, "end_time": query_end},
    )
    still_there = [rec for rec in r.json() if source_id in (rec.get("sources") or [])]
    print(f"  event still present after def-delete: {bool(still_there)}")

    print()
    print("=== SUMMARY ===")
    for f in findings:
        print(f"  {f['method']:18} {f['path']} -> {f['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
