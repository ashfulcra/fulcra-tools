"""Runs INSIDE the OS sandbox. Modes: verbs (the clean run) | attack (the battery) | mutate (file list
through its own CLI) | epoch (get-records from the epoch) | probe (2000 guessed stats). Every mutation
must be FLAGGED by the request log. Prints one JSON line."""
import io
import json
import socket
import subprocess
import sys

sock, mode, corpus_path = sys.argv[1], sys.argv[2], sys.argv[3]
from coord_fold import fold
from coord_fold.cli import main
from coord_fold.transport import CliPointerReader, CliPointerWriter

HERE = __file__.rsplit("/", 1)[0]
cli = [sys.executable, HERE + "/fake_cli.py", sock]
r, w = CliPointerReader(cli=cli), CliPointerWriter(cli=cli)
VERBS = [("fold", ["fold", "r", "--agent", "me", "--now", "2026-09-04T11:00:00Z"]),
         ("status", ["status", "r", "--agent", "me"]),
         ("emit", ["emit", "r", "--from", "me", "--to", "boss", "--kind", "note", "--slug", "s0", "--pri", "P3", "--at", "2026-09-04T11:01:00Z"]),
         ("claim", ["claim", "r", "s1", "--agent", "me", "--at", "2026-09-04T11:02:00Z"]),
         ("release", ["release", "r", "s2", "--agent", "me", "--at", "2026-09-04T11:03:00Z"]),
         ("close", ["close", "r", "s0", "--agent", "me", "--evidence", "team/r/_coord/responses/s0/reply.md", "--at", "2026-09-04T11:04:00Z"]),
         ("fold2", ["fold", "r", "--agent", "me", "--now", "2026-09-04T12:00:00Z"])]
out = {"mode": mode}
if mode == "epoch":
    from coord_fold import transport
    real_records = transport.CliPointerReader._records
    def epoch_records(self, channel, since):
        return real_records(self, channel, "1970-01-01T00:00:00Z")     # codex-coder round 9: enumeration downstream of an allowed name
    transport.CliPointerReader._records = epoch_records
if mode == "probe":
    real = fold.run
    def probing(reader, writer, team, agent, **kw):
        for i in range(2000):                                     # codex-coder round 10: point-probe a namespace through an allowed name
            reader.read_classified(f"team/{team}/task/guess-{i}.md")
        return real(reader, writer, team, agent, **kw)
    fold.run = probing
if mode == "mutate":
    real = fold.run
    def enumerating(reader, writer, team, agent, **kw):
        subprocess.run([*cli, "file", "list", f"team/{team}/"], capture_output=True)   # the only road to a store is observed
        return real(reader, writer, team, agent, **kw)
    fold.run = enumerating
if mode in ("verbs", "mutate", "epoch", "probe"):
    out["rc"] = {label: main(argv, reader=r, writer=w) for label, argv in VERBS}
if mode == "attack":
    res = {}
    def t(label, fn):
        try:
            fn()
            res[label] = "NOT denied"
        except BaseException as e:
            res[label] = f"denied {type(e).__name__}"
    import posix
    t("codex-reviewer: reader._s.events", lambda: r._s.events)
    t("codex-reviewer: io.open('/etc/hosts')", lambda: io.open("/etc/hosts").read(4))
    t("codex-coder: original posix.listdir('/Users')", lambda: posix.listdir("/Users"))
    t("read the corpus file", lambda: open(corpus_path).read(4))
    t("subprocess /bin/ls", lambda: subprocess.run(["/bin/ls", "/"], capture_output=True))
    t("outbound socket 192.0.2.1:53 (RFC 5737 documentation address: unroutable; the seatbelt denies the connect regardless of destination)", lambda: socket.create_connection(("192.0.2.1", 53), timeout=2))
    def direct():
        c = socket.socket(socket.AF_UNIX); c.connect(sock); c.sendall(b'{"argv":["file","list","team/r/"]}\n')
        reply = json.loads(c.recv(1 << 16)); assert reply["rc"] == 2, reply; raise PermissionError("store refused: " + reply["stderr"].strip())
    t("direct socket asking the store for file list", direct)
    out["attack"] = res
    out["test_detectable"] = type(r).__module__     # stated, not hidden: a fold CAN see this is a CliPointerReader
print(json.dumps(out))
