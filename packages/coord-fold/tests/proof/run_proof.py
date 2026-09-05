"""THE PROOF (G29). Exit 0 = proven for this run; 1 = failed; 3 = UNKNOWN — no OS sandbox on this
host. UNKNOWN is never green: CI must run this on a host that has one (macOS seatbelt today; a
Linux bwrap profile is an open infrastructure ask)."""
import calendar
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent.parent
PROFILE = """(version 1)
(allow default)
(deny network*)
(allow network-outbound (literal "{sock}"))
(deny process-exec (with no-log))
(allow process-exec (subpath "{pyprefix}") (subpath "{venv}"))
(deny file-read-data (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/tmp") (subpath "/private/etc") (subpath "/etc") (subpath "/private/var") (subpath "/var") (subpath "/Applications") (subpath "/Volumes") (subpath "/Library"))
(allow file-read-data (subpath "{pyprefix}") (subpath "{venv}") (subpath "{pkg}") (subpath "{tmp}"))
(deny file-write* (subpath "/Users") (subpath "/home") (subpath "/private/tmp") (subpath "/tmp") (subpath "/dev"))
(allow file-write* (subpath "{tmp}") (literal "/dev/tty") (literal "/dev/null"))
"""
ALLOWED = {("file", "stat"), ("file", "download"), ("get-records",), ("record",), ("file", "upload")}
CFG = "team/r/_coord/bus-v4/records.json"
CKPT = "team/r/member/me/fold/checkpoint.json"
EVIDENCE = "team/r/_coord/responses/s0/reply.md"
ALLOWED_PATHS = {CFG, CKPT, EVIDENCE}                 # the ONLY paths the clean run may stat/download/upload
MAX_PER_SHAPE = {("file", "stat"): 20, ("file", "download"): 20, ("file", "upload"): 4, ("record",): 4, ("get-records",): 2}
# The exact clean-run request sequence, MEASURED from the proof (upload temp paths and since values normalised).
# A change to the fold changes this list deliberately, in the same commit, with the new measurement.
EXPECTED_SEQUENCE = [
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "get-records",
    "MomentAnnotation/x",
    "<since>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "upload",
    "<tmp>",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/responses/s0/reply.md"
    ],
    [
    "file",
    "download",
    "team/r/_coord/responses/s0/reply.md",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "record"
    ],
    [
    "file",
    "stat",
    "team/r/_coord/bus-v4/records.json"
    ],
    [
    "file",
    "download",
    "team/r/_coord/bus-v4/records.json",
    "/dev/stdout"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "get-records",
    "MomentAnnotation/x",
    "<since>"
    ],
    [
    "file",
    "stat",
    "team/r/member/me/fold/checkpoint.json"
    ],
    [
    "file",
    "download",
    "team/r/member/me/fold/checkpoint.json",
    "/dev/stdout"
    ],
    [
    "file",
    "upload",
    "<tmp>",
    "team/r/member/me/fold/checkpoint.json"
    ]
    ]


CORPUS_N, OVERLAP = 5000, 5                       # large corpus; OVERLAP must equal coord_fold.fold.OVERLAP_SECONDS
BASE_EPOCH = 1788512400                           # 2026-09-04T09:00:00Z (calendar.timegm)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def corpus():
    ev = []
    for i in range(CORPUS_N):                     # one record per second; the first five are addressed to me, the rest to others
        at = _iso(BASE_EPOCH + i)
        to = "me" if i < 5 else "them"
        ev.append({"id": str(i), "recorded_at": at, "note": json.dumps({"v": 1, "at": at, "from": "boss", "to": to, "kind": "open", "slug": f"s{i}", "pri": "P1", "ptr": f"team/r/task/s{i}.md"})})
    return {"docs": {CFG: json.dumps({"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}), "team/r/_coord/responses/s0/reply.md": "done"}, "events": ev}


LAST_CORPUS_AT = _iso(BASE_EPOCH + CORPUS_N - 1)
EXPECTED_SINCE_2 = _iso(BASE_EPOCH + CORPUS_N - 1 - OVERLAP)   # the second fold must ask from the last observed record minus the overlap
BOUNDED_RETURN = OVERLAP + 1 + 4                                # overlap window + the boundary record + the four records the verbs wrote


def shape(argv):
    return tuple(argv[:2]) if argv[:1] == ["file"] else tuple(argv[:1])


def norm(argv):
    if argv[:2] == ["file", "upload"]:
        return ["file", "upload", "<tmp>", argv[3]]
    if argv[:1] == ["get-records"]:
        return ["get-records", argv[1], "<since>"]
    return argv


def path_of(argv):
    if argv[:2] in (["file", "stat"], ["file", "download"]):
        return argv[2]
    if argv[:2] == ["file", "upload"]:
        return argv[3]
    return None


def reads(reqs):
    """(channel, since, returned) for every get-records the store served, in order."""
    return [(r["argv"][1], r["argv"][2], r["returned"]) for r in reqs if r["argv"][:1] == ["get-records"]]


def main() -> int:
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        print("PROOF UNKNOWN (rc 3): no OS sandbox on this host — this is not a pass and must never be softened to a skip; the gate is a PASSED record from a host that has one")
        return 3
    private = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="coord-fold-proof-")))   # realpath: seatbelt matches RESOLVED paths (/tmp -> /private/tmp); under /private/tmp: DENIED to the fold
    tmp = private / "sandbox-tmp"; tmp.mkdir()                                  # the fold's only writable place (allowed after the deny)
    sock = f"/private/tmp/cf-{os.getpid()}.sock"                                 # AF_UNIX paths must stay short
    log, corpus_path, profile = private / "argv.jsonl", private / "corpus.json", private / "profile.sb"
    corpus_path.write_text(json.dumps(corpus()))
    pyprefix = os.path.realpath(sys.base_prefix)
    # A venv interpreter reads <venv>/pyvenv.cfg at startup (site.venv); on CI that file lives under the repo's .venv,
    # outside the package tree, and the kernel deny aborted the interpreter before phase 1 (measured on the macOS runner).
    venv = os.path.realpath(sys.prefix)
    profile.write_text(PROFILE.format(sock=sock, pyprefix=pyprefix, venv=venv, pkg=os.path.realpath(PKG), tmp=tmp))
    server = subprocess.Popen([sys.executable, str(HERE / "store_server.py"), sock, str(log), str(corpus_path)], stdout=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    env = {"PYTHONPATH": str(PKG), "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp), "HOME": str(tmp), "PATH": "/usr/bin:/bin"}
    failures = []

    def run(mode):
        before = log.read_text().count("\n") if log.exists() else 0
        p = subprocess.run(["sandbox-exec", "-f", str(profile), sys.executable, str(HERE / "inside.py"), sock, mode, str(corpus_path)],
                           capture_output=True, text=True, env=env, cwd=PKG)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        result = json.loads(lines[-1]) if lines else {"error": p.stderr[-800:]}
        result["stderr_tail"] = p.stderr[-1200:]
        requests = [json.loads(l) for l in log.read_text().splitlines()[before:]] if log.exists() else []
        return result, requests

    def enumerating(reqs):
        """A request enumerates if its verb is not allowed OR an allowed verb is used with enumerating semantics."""
        bad = [r["argv"] for r in reqs if shape(r["argv"]) not in ALLOWED]
        for r in reqs:
            if r["argv"][:1] != ["get-records"]:
                continue
            held = r.get("ckpt_cursor")
            floor = _iso(calendar.timegm(time.strptime(held, "%Y-%m-%dT%H:%M:%SZ")) - OVERLAP) if held else None
            if r["argv"][1] != "MomentAnnotation/x" or (floor is not None and r["argv"][2] < floor):
                bad.append(r["argv"] + [f"returned={r['returned']}", f"held_cursor={held}"])   # reading behind the checkpoint the store holds = re-reading the corpus
        bad += [r["argv"] for r in reqs if path_of(r["argv"]) not in (None, *ALLOWED_PATHS)]          # point-probing a namespace (codex-coder round 10)
        counts = {}
        for r in reqs:
            counts[shape(r["argv"])] = counts.get(shape(r["argv"]), 0) + 1
        bad += [[f"{k}: {v} requests > bound {MAX_PER_SHAPE.get(k, 0)}"] for k, v in counts.items() if v > MAX_PER_SHAPE.get(k, 0)]
        return bad

    try:
        res, reqs = run("verbs")
        bad_rc = {k: v for k, v in res.get("rc", {}).items() if v != 0}
        shapes = {shape(r["argv"]) for r in reqs}
        rd = reads(reqs)
        first_ok = len(rd) == 2 and rd[0][0] == "MomentAnnotation/x" and rd[0][2] == CORPUS_N                    # first observation reads the whole corpus, once
        second_ok = len(rd) == 2 and rd[1] == ("MomentAnnotation/x", EXPECTED_SINCE_2, rd[1][2]) and rd[1][2] <= BOUNDED_RETURN   # G31: cursor semantics, bounded return
        print(f"phase 1 clean run: rc={res.get('rc', res)} requests={len(reqs)} shapes={sorted(shapes)} get-records={rd} expected second since={EXPECTED_SINCE_2} bounded<={BOUNDED_RETURN}")
        observed = [norm(r["argv"]) for r in reqs]
        if observed != EXPECTED_SEQUENCE:
            print("phase 1: request sequence differs from the measured expectation:")
            for i, (a, b) in enumerate(zip(observed + [None] * len(EXPECTED_SEQUENCE), EXPECTED_SEQUENCE + [None] * len(observed))):
                if a != b:
                    print(f"  #{i}: observed={a} expected={b}")
        if bad_rc or not reqs or not shapes <= ALLOWED or not first_ok or not second_ok or observed != EXPECTED_SEQUENCE or enumerating(reqs):
            failures.append("phase 1")
            print("phase 1 enumerating:", enumerating(reqs)[:3], "| stderr tail:", res.get("stderr_tail", "")[-600:])
        res, reqs = run("attack")
        print("phase 2 attack battery:", json.dumps(res.get("attack", res), indent=1), "| the fold can see:", res.get("test_detectable"))
        if any(not v.startswith("denied") for v in res.get("attack", {}).values()) or not res.get("attack"):
            failures.append("phase 2")
        res, reqs = run("mutate")
        flagged = enumerating(reqs)
        print(f"phase 3 mutated fold (file list through its own CLI): FLAGGED = {flagged[:2]}")
        if not flagged:
            failures.append("phase 3 (the mutation was not detected — the proof cannot fail, so it proves nothing)")
        res, reqs = run("epoch")
        flagged = enumerating(reqs)
        print(f"phase 4 epoch-rewritten production reader (codex-coder round 9): get-records={reads(reqs)} FLAGGED = {bool(flagged)}")
        if not flagged:
            failures.append("phase 4 (an allowed verb with enumerating semantics was not detected)")
        res, reqs = run("probe")
        flagged = enumerating(reqs)
        print(f"phase 5 point-probing production reader (codex-coder round 10): requests={len(reqs)} FLAGGED = {bool(flagged)} e.g. {flagged[:1]}")
        if not flagged:
            failures.append("phase 5 (point-probing through an allowed name was not detected)")
    finally:
        server.terminate()
        shutil.rmtree(private, ignore_errors=True)
        if os.path.exists(sock):
            os.unlink(sock)
    if failures:
        print("PROOF FAILED:", failures)
        return 1
    import platform
    print(f"PROOF PASSED on host={platform.node()} platform={platform.platform()} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: "
          "these specific capabilities were denied by the kernel and the fold still completed; the clean run's request sequence matched the measured expectation; all three mutated folds were flagged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
