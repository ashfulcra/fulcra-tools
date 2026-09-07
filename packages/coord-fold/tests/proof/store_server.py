"""THE STORE (G29), outside the sandbox. Holds the corpus (loaded from a file the sandboxed fold
cannot read); answers ONLY the five fixed request shapes; LOGS EVERY REQUEST; refuses the rest.
Nothing in the fold's process holds the corpus, so there is nothing there to walk."""
import json
import os
import socket
import sys
import threading

sock_path, log_path, corpus_path = sys.argv[1], sys.argv[2], sys.argv[3]
store = json.load(open(corpus_path))          # {"docs": {path: text}, "events": [record, ...]}
lock = threading.Lock()


CKPT_SUFFIX = "/fold/checkpoint.json"


def held_cursor():
    """The cursor of the checkpoint THE STORE holds right now (None before the first save)."""
    for path, text in store["docs"].items():
        if path.endswith(CKPT_SUFFIX):
            try:
                return json.loads(text).get("cursor")
            except (ValueError, AttributeError):
                return None
    return None


def log(argv, returned=None, ckpt_cursor=None):
    with lock, open(log_path, "a") as f:
        f.write(json.dumps({"argv": argv, "returned": returned, "ckpt_cursor": ckpt_cursor}) + "\n")


def handle(req):
    argv, stdin = req["argv"], req.get("stdin", "")
    if argv[:1] == ["get-records"]:                       # SEMANTICS are logged, not just the verb (codex-coder round 9)
        since = argv[2]
        hits = [e for e in store["events"] if e["recorded_at"] >= since]
        log(argv, returned=len(hits), ckpt_cursor=held_cursor())
        return (0, "".join(json.dumps(e) + "\n" for e in hits), "")
    log(argv)
    if argv[:2] == ["file", "stat"]:
        p = argv[2]
        return (0, f"/{p} ({len(store['docs'][p])} bytes)\n", "") if p in store["docs"] else (1, "", f"Error: File not found in Fulcra: /{p}\n")
    if argv[:2] == ["file", "download"]:
        p = argv[2]
        return (0, store["docs"][p], "") if p in store["docs"] else (1, "", "Error: File not found\n")
    if argv[:1] == ["record"]:
        # The REAL CLI's refusals, reproduced (rule: a fake must refuse what the real one refuses). `fulcra-api record`
        # is `record DATA_TYPE [VALUE] --api-version V --source S`; no positional -> usage error rc 2; a data type is
        # never a flag. Measured live 2026-09-05: "Error: Missing argument 'DATA_TYPE'."
        if len(argv) < 2 or argv[1].startswith("-"):
            return (2, "", "Usage: fulcra record [OPTIONS] DATA_TYPE [VALUE]\nError: Missing argument 'DATA_TYPE'.\n")
        if "--api-version" not in argv or "--source" not in argv:
            return (2, "", "Error: Missing option '--api-version' / '--source'.\n")
        doc = json.loads(stdin)
        if set(doc) - {"note", "recorded_at", "tags"}:
            return (2, "", f"Error: unknown record fields {sorted(set(doc) - {'note', 'recorded_at', 'tags'})}\n")
        with lock:
            store["events"].append({"id": f"w{len(store['events'])}", "recorded_at": doc["recorded_at"], "note": doc["note"]})
        return (0, "recorded\n", "")
    if argv[:2] == ["file", "upload"]:
        with lock:
            store["docs"][argv[3]] = req.get("upload_body", "")
        return (0, "uploaded\n", "")
    return (2, "", f"REFUSED: {argv[:2]} is not a supported request\n")


def serve(conn):
    with conn:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            data += chunk
        rc, out, err = handle(json.loads(data))
        conn.sendall((json.dumps({"rc": rc, "stdout": out, "stderr": err}) + "\n").encode())


if os.path.exists(sock_path):
    os.unlink(sock_path)
srv = socket.socket(socket.AF_UNIX)
srv.bind(sock_path)
srv.listen(16)
print("store server up", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
