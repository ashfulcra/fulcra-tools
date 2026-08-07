"""Host-local wake adapter `opencode-wake`: the OpenCode row of the harness
matrix.

The adapter posts ONE fixed, content-free nudge prompt to a PINNED session on
a self-hosted, loopback-only `opencode serve` (POST /session/<id>/prompt_async)
when the router's invoker passes exactly --agent/--key/--reason. The durable
bus shards stay authoritative; the nudge carries no work content.

The load-bearing properties pinned here, all WITHOUT any opencode install:

- ARGV CONTRACT: unknown/extra args exit 2; hostile agent ids and keys are
  refused by charset before anything runs (same gating as macos-notify.sh and
  the engine's invoker).
- CREDENTIAL HYGIENE (the novel risk): the serve password is read at
  invocation time from a mode-600 file (inline or via PASSWORD_FILE) and
  reaches curl ONLY via a config document on stdin (`-K -`) — never in argv
  (argv is world-readable via ps), never in the JSON body, never logged.
- CONFIG DISCIPLINE: missing/world-readable config or password files, unknown
  keys, and non-loopback HOST all fail closed with exit 2.
- DELIVERY SEMANTICS: HTTP 204 -> exit 0; anything else / unreachable ->
  bounded retries then exit 1; an explicitly busy session coalesces the nudge
  (exit 0, no POST) because that session's standing orders read the full inbox
  every turn and the nudge carries no content to lose.
- RUNTIME ABSENCE: no curl or no python3 -> exit 127, fast.

Every test here runs against a RECORDING curl shim on PATH — no opencode
server, no network beyond nothing. What this suite CANNOT prove (stated in
docs/coord/wake-opencode.md): that a real `opencode serve` accepts the POST,
and that the pinned session actually wakes — that is covered by the live
evidence section of the doc, not by this suite.
"""

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills/fulcra-agent-automation/scripts/wake/opencode-wake.sh"

AGENT = "Opencode-Kimi-Coder"
KEY = "dir-123:Opencode-Kimi-Coder"
REASON = "directed item on your bus - check your inbox / needs-me"
SECRET = "deadbeefcafe1234567890"  # stands in for the serve password
SID = "ses_testsession123"

BASH = shutil.which("bash") or "/bin/bash"


# --- fixtures ----------------------------------------------------------------

def _write_config(tmp_path, body=None, mode=0o600, pw_mode=0o600,
                  inline=False):
    """Provision a config (+ password file) and return its path."""
    pw = tmp_path / "serve-password"
    pw.write_text(SECRET + "\n")
    pw.chmod(pw_mode)
    if body is None:
        body = (
            "PORT=4196\n"
            f"{'PASSWORD=' + SECRET if inline else 'PASSWORD_FILE=' + str(pw)}\n"
            f"SESSION_ID={SID}\n"
        )
    cfg = tmp_path / "serve-session"
    cfg.write_text(body)
    cfg.chmod(mode)
    return cfg


CURL_SHIM = r"""#!/bin/sh
# Recording curl shim. Logs argv/stdin/payload/url; answers per URL suffix.
out="${SHIM_RECORD_DIR:?set SHIM_RECORD_DIR}"
printf '%s\n' "$@" > "$out/argv.txt"
# -K - means: read the config document from stdin
prev=""
want_stdin=0
payload=""
url=""
for a in "$@"; do
  if [ "$prev" = "-K" ] && [ "$a" = "-" ]; then want_stdin=1; fi
  if [ "$prev" = "--data-binary" ]; then payload="$a"; fi
  prev="$a"
  case "$a" in http*) url="$a" ;; esac
done
if [ "$want_stdin" = "1" ]; then cat > "$out/stdin.txt"; fi
printf '%s' "$payload" > "$out/payload.txt"
printf '%s\n' "$url" >> "$out/urls.txt"
case "$url" in
  */session/status)
    [ -n "${SHIM_STATUS_JSON:-}" ] && printf '%s' "$SHIM_STATUS_JSON"
    ;;
  *)
    printf '%s' "${SHIM_HTTP_CODE:-204}"
    ;;
esac
exit "${SHIM_EXIT:-0}"
"""


def _shim_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    shim = d / "curl"
    shim.write_text(CURL_SHIM)
    shim.chmod(0o755)
    return d


def _run(argv, *, env_extra=None, path=None, timeout=20):
    env = {"PATH": path or os.environ["PATH"], "HOME": "/nonexistent"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def _run_adapter(tmp_path, cfg, *extra_env, args=None, shim=True):
    record = tmp_path / "rec"
    record.mkdir(exist_ok=True)
    env = {"OPENCODE_WAKE_CONFIG": str(cfg),
           "SHIM_RECORD_DIR": str(record),
           "COORD_OPENCODE_WAKE_ATTEMPTS": "1"}
    for kv in extra_env:
        k, _, v = kv.partition("=")
        env[k] = v
    path = os.environ["PATH"]
    if shim:
        path = f"{_shim_dir(tmp_path)}:{path}"
    argv = [str(SCRIPT), *args] if args is not None else [
        str(SCRIPT), "--agent", AGENT, "--key", KEY, "--reason", REASON]
    r = _run(argv, env_extra=env, path=path)
    return r, record


# --- script surface ----------------------------------------------------------

def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "adapter script is not executable"


@pytest.mark.parametrize("argv", [
    [],
    ["--agent", AGENT],
    ["--agent", AGENT, "--key", KEY],
    ["--agent", "bad;agent", "--key", KEY, "--reason", "r"],
    ["--agent", "-leadingdash", "--key", KEY, "--reason", "r"],
    ["--agent", AGENT, "--key", "bad key", "--reason", "r"],
    ["--agent", AGENT, "--key", KEY, "--reason", "r", "--command", "rm -rf /"],
    ["--agent", AGENT, "--key", KEY, "--reason", "r", "--url",
     "http://evil.example"],
])
def test_refuses_bad_or_unknown_arguments(tmp_path, argv):
    """Usage/charset errors exit 2 WITHOUT running curl — notably unknown
    `--command` / `--url` flags: this adapter has no command surface."""
    cfg = _write_config(tmp_path)
    r, record = _run_adapter(tmp_path, cfg, args=argv)
    assert r.returncode == 2, (argv, r.stderr)
    assert not (record / "argv.txt").exists(), "curl ran on a rejected argv"


def test_reason_over_200_chars_refused(tmp_path):
    cfg = _write_config(tmp_path)
    r, record = _run_adapter(tmp_path, cfg,
                             args=[str(SCRIPT), "--agent", AGENT, "--key",
                                   KEY, "--reason", "x" * 201])
    assert r.returncode == 2
    assert not (record / "argv.txt").exists()


# --- runtime absence ---------------------------------------------------------

def test_missing_curl_is_127_fast(tmp_path):
    empty = tmp_path / "nobin"
    empty.mkdir()
    t0 = time.monotonic()
    r = _run([BASH, str(SCRIPT), "--agent", AGENT, "--key", KEY,
              "--reason", "r"], path=str(empty))
    assert r.returncode == 127
    assert "curl" in r.stderr
    assert time.monotonic() - t0 < 10


def test_missing_python3_is_127(tmp_path):
    only_curl = _shim_dir(tmp_path)
    r = _run([BASH, str(SCRIPT), "--agent", AGENT, "--key", KEY,
              "--reason", "r"],
             env_extra={"SHIM_RECORD_DIR": str(tmp_path)},
             path=str(only_curl))
    assert r.returncode == 127
    assert "python3" in r.stderr


# --- config / credential discipline ------------------------------------------

def test_missing_config_is_rc2(tmp_path):
    r, _ = _run_adapter(tmp_path, tmp_path / "does-not-exist")
    assert r.returncode == 2
    assert "config" in r.stderr


def test_world_readable_config_refused(tmp_path):
    cfg = _write_config(tmp_path, mode=0o644)
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "0600" in r.stderr
    assert not (record / "argv.txt").exists()


def test_world_readable_password_file_refused(tmp_path):
    cfg = _write_config(tmp_path, pw_mode=0o644)
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "password file" in r.stderr
    assert not (record / "argv.txt").exists()


def test_unknown_config_key_refused(tmp_path):
    cfg = _write_config(tmp_path, body="PORT=4196\nPASSWORD=x\n"
                                       "SESSION_ID=s\nEVIL=1\n")
    r, _ = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "unsupported key" in r.stderr


def test_both_password_forms_refused(tmp_path):
    cfg = _write_config(
        tmp_path,
        body="PORT=4196\nPASSWORD=x\nPASSWORD_FILE=/tmp/p\nSESSION_ID=s\n")
    r, _ = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "both" in r.stderr


def test_non_loopback_host_refused(tmp_path):
    cfg = _write_config(tmp_path, body="PORT=4196\nPASSWORD=x\n"
                                       "SESSION_ID=s\nHOST=10.0.0.5\n")
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "loopback" in r.stderr
    assert not (record / "argv.txt").exists()


def test_password_with_curl_config_metachars_refused(tmp_path):
    cfg = _write_config(tmp_path)
    # overwrite the provisioned password with a hostile one AFTER the fixture
    # has written the default (fixture writes the password file too)
    pw = tmp_path / "serve-password"
    pw.write_text('bad"quote\n')
    pw.chmod(0o600)
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 2
    assert "unsupported characters" in r.stderr
    assert not (record / "argv.txt").exists()


# --- delivery semantics + the credential boundary -----------------------------

def test_happy_path_204_and_password_never_in_argv_or_body(tmp_path):
    cfg = _write_config(tmp_path)
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 0, r.stderr
    assert "HTTP 204" in r.stdout
    argv_text = (record / "argv.txt").read_text()
    urls = (record / "urls.txt").read_text()
    # the password crossed on STDIN (curl -K - config document)...
    assert SECRET in (record / "stdin.txt").read_text()
    # ...and NOWHERE else: not argv (ps-readable), not the body, not stdout/err
    assert SECRET not in argv_text
    assert SECRET not in (record / "payload.txt").read_text()
    assert SECRET not in r.stdout and SECRET not in r.stderr
    # the fixed nudge shape: agent + key, loopback prompt_async endpoint
    body = json.loads((record / "payload.txt").read_text())
    assert body["agent"] == "bus-runner"
    text = body["parts"][0]["text"]
    assert AGENT in text and KEY in text and REASON in text
    assert "prompt_async" in urls
    assert f"/session/{SID}/" in urls
    assert "127.0.0.1" in urls


def test_inline_password_form_also_stays_out_of_argv(tmp_path):
    cfg = _write_config(tmp_path, inline=True)
    r, record = _run_adapter(tmp_path, cfg)
    assert r.returncode == 0, r.stderr
    assert SECRET not in (record / "argv.txt").read_text()
    assert SECRET in (record / "stdin.txt").read_text()


def test_busy_session_coalesces_without_posting(tmp_path):
    cfg = _write_config(tmp_path)
    r, record = _run_adapter(
        tmp_path, cfg, f'SHIM_STATUS_JSON={{"{SID}": {{"type": "busy"}}}}')
    assert r.returncode == 0, r.stderr
    assert "coalesced" in r.stdout
    urls = (record / "urls.txt").read_text()
    assert "prompt_async" not in urls, "busy session still got a POST"


def test_non_204_is_delivery_failure_rc1(tmp_path):
    cfg = _write_config(tmp_path)
    r, _ = _run_adapter(tmp_path, cfg, "SHIM_HTTP_CODE=500")
    assert r.returncode == 1
    assert "not delivered" in r.stderr


def test_unreachable_server_is_rc1(tmp_path):
    cfg = _write_config(tmp_path)
    r, _ = _run_adapter(tmp_path, cfg, "SHIM_EXIT=7")
    assert r.returncode == 1


def test_structural_no_secret_or_eval_in_script():
    body = SCRIPT.read_text()
    for forbidden in ("eval ", "curl -u", "--user ", "source "):
        assert forbidden not in body, f"{forbidden!r} in the adapter"


def test_shellcheck_clean_if_available():
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, str(SCRIPT)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
