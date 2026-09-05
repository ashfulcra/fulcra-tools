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
sys.stdout.write(r["stdout"])
sys.stderr.write(r["stderr"])
sys.exit(r["rc"])
