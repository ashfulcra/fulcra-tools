# fulcra-coord-files

No-CAS object-store transport for the Fulcra coordination bus, extracted from
`fulcra-coord` so the event-sourcing layer can depend on a small, documented
store contract instead of the whole coordination package.

## What this package is

A thin, behavior-identical wrapper around the Fulcra Files CLI (`fulcra file
upload/download/stat/list/delete`). Every operation shells out to a backend
command; tests inject a fake backend via the `backend=` argument or the
`FULCRA_COORD_BACKEND` environment variable.

## The NO-CAS contract (read this before building on it)

The underlying store has **no compare-and-swap**. There is no atomic
"write-if-unchanged". The consequences shape every safe usage pattern:

- The durable unit is an **immutable, uniquely-named blob** that is never
  overwritten. Concurrency safety comes from each writer owning a distinct path
  (per-agent presence files, per-id archive shards, per-day rolling markers),
  not from locking a shared mutable file.
- `stat` / version is a **staleness hint, not a correctness guarantee**. A
  matching version is evidence (not proof) that nothing changed; a differing
  version is proof that something did. Read-modify-write on a shared path is
  inherently racy here and must be avoided.

## Public surface

```python
import fulcra_coord_files as files

files.upload_json(data, "/coordination/x.json", backend=B)
files.download_json("/coordination/x.json", backend=B)
files.list_json("/coordination/events/tasks/T1/", backend=B)
```

Exported: `stat`, `download`, `download_json`, `upload`, `upload_json`,
`delete`, `list_files`, `list_json`, `stat_changed`, `check_cli_available`,
`check_file_commands`, `probe_reachable`, `check_remote_access`.

### Failure observability: `store.last_upload_error`

`fulcra_coord_files.store.last_upload_error` holds the stderr tail (last 200
chars) of the most recent **failed** upload — `None` until an upload fails. It
exists because `upload` returns a bare bool used by dozens of callers (a richer
return would ripple everywhere) and the transport must stay free of any logging
import back into `fulcra-coord`. Read it as a **live module attribute**
immediately after a `False` return (re-exports of the value won't track
mutation). Best-effort by design: never cleared on success, and under parallel
uploads the last failing writer wins — a diagnostic hint, not per-call truth.

## Back-compat

`fulcra_coord.remote` re-exports every symbol moved here, so existing callers
and the test patch surface (`fulcra_coord.remote.upload_json`, etc.) keep
resolving unchanged.

## Test

```
uv run --all-packages --all-extras --no-editable --with pytest \
    python -m pytest packages/fulcra-coord-files/tests -q
```
