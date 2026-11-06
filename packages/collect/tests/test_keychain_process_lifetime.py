"""Synthetic process lifetime acceptance: no native Keychain or network access."""
import os
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent lifetime")
def test_isolated_helper_exits_when_requesting_parent_dies(tmp_path):
    ready, pid_file = tmp_path / "ready", tmp_path / "pid"
    child = tmp_path / "helper_child.py"
    child.write_text('''import sys,time,types,socket
from pathlib import Path
import keyring
def deny(*a,**k):raise AssertionError('native/auth/network forbidden')
for n in ['get_password','set_password','delete_password']:setattr(keyring,n,deny)
socket.create_connection=deny
from fulcra_collect import macos_keychain as m
m._api=lambda:types.SimpleNamespace(disable_interaction=lambda:None)
def read(*a,**k):
 Path(sys.argv[1]).write_text('ready')
 time.sleep(60)
m._read_password=read
raise SystemExit(m.child_main())
''')
    parent_code = '''import subprocess,sys,time,json
from pathlib import Path
p=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],stdin=subprocess.PIPE,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
p.stdin.write(json.dumps({'service':'fulcra-collect:synthetic','account':'synthetic'}).encode())
p.stdin.close()
Path(sys.argv[3]).write_text(str(p.pid))
time.sleep(60)
'''
    env = dict(os.environ, FULCRA_COLLECT_HOME=str(tmp_path), FULCRA_ACCESS_TOKEN="synthetic")
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(child), str(ready), str(pid_file)], env=env)
    helper_pid = None
    def running(pid):
        r = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    try:
        deadline = time.monotonic() + 6
        while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        helper_pid = int(pid_file.read_text())
        parent.terminate()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 2
        while running(helper_pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not running(helper_pid), "quiet helper survived its requesting process"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        if helper_pid is not None and running(helper_pid):
            r = subprocess.run(["/bin/ps", "-p", str(helper_pid), "-o", "args="], capture_output=True, text=True)
            if str(child) in r.stdout:
                os.kill(helper_pid, signal.SIGKILL)
