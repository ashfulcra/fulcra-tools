"""The thin client the PRODUCTION reader/writer exec as their `cli`: argv -> unix socket -> store."""
import json
import socket
import sys

sock_path, argv = sys.argv[1], sys.argv[2:]
req = {"argv": argv}
if argv[:1] == ["record"]:
    req["stdin"] = sys.stdin.read()
if argv[:2] == ["file", "upload"]:
    req["upload_body"] = open(argv[2]).read()      # the writer's own temp file, under its TMPDIR
c = socket.socket(socket.AF_UNIX)
c.connect(sock_path)
c.sendall((json.dumps(req) + "\n").encode())
data = b""
while not data.endswith(b"\n"):
    chunk = c.recv(1 << 20)
    if not chunk:
        break
    data += chunk
r = json.loads(data)
if argv[:2] == ["file", "download"]:
    # Behave like the REAL fulcra-api (measured 2026-09-05): LOCAL_FILE is validated as a readable path, so
    # /dev/stdout under a pipe is REFUSED, and a successful download writes the body to LOCAL_FILE — never stdout.
    # The old fake printed the body to stdout, which is how the production reader's /dev/stdout form passed the
    # proof and then refused every real fold at the channel config.
    if len(argv) < 4 or argv[3] == "/dev/stdout":
        sys.stderr.write("Error: Invalid value for '[LOCAL_FILE]': Path '/dev/stdout' is not readable.\n")
        sys.exit(2)
    if r["rc"] == 0:
        with open(argv[3], "w") as f:                 # the reader's own private temp file, under its TMPDIR
            f.write(r["stdout"])
        sys.exit(0)
sys.stdout.write(r["stdout"])
sys.stderr.write(r["stderr"])
sys.exit(r["rc"])
