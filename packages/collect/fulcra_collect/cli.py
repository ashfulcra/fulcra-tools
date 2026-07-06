"""The `fulcra-collect` command-line interface.

`daemon` and `install` act locally; the rest talk to a running daemon
over the control socket. `_worker` is the internal worker entrypoint.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys

import click

from . import config as config_mod
from . import credentials, worker
from .control import send_request
from .daemon import Daemon


def _socket_path():
    return config_mod.config_dir() / "control.sock"


def _configure_logging() -> None:
    """Install a single stderr handler on the root logger at INFO.

    Without this the daemon ran with no root handler, so INFO records
    (the ``web UI: ...`` startup line, anything below WARNING) hit
    Python's last-resort handler and were dropped — ``daemon.out.log``
    stayed empty even while the server answered requests, which hid live
    401s. launchd captures stderr to ``~/Library/Logs/fulcra-collect/``,
    so a stderr handler is what surfaces in the log files.

    Level is overridable via ``FULCRA_COLLECT_LOG_LEVEL`` (e.g. DEBUG).
    Idempotent: repeated calls re-use the handler we tagged rather than
    stacking duplicates, so restarting the server in-process stays clean.
    """
    level_name = os.environ.get("FULCRA_COLLECT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "_fulcra_collect", False):
            handler.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler._fulcra_collect = True  # type: ignore[attr-defined]
    root.addHandler(handler)


@click.group()
def cli() -> None:
    """Background hub for the Fulcra local helpers."""


def _stderr_is_a_tty() -> bool:
    """Module-level seam so tests can mock the TTY check; CliRunner swaps
    sys.stderr to a buffer at invoke time, so monkeypatching sys.stderr.isatty
    from a test doesn't reach the daemon command."""
    return sys.stderr.isatty()


@cli.command()
def daemon() -> None:
    """Run the hub core in the foreground (the launchd/systemd entrypoint)."""
    _configure_logging()
    # When launched interactively (not under launchd), print a one-line hint
    # so first-time operators know they're running a non-durable foreground
    # daemon and where to find the persistent option. launchd has no
    # controlling TTY, so the TTY check guards us from spamming the log file.
    if _stderr_is_a_tty():
        click.echo(
            "running in the foreground (this daemon will die when you close "
            "this terminal). For a persistent daemon: `fulcra-collect install` "
            "then `launchctl bootstrap gui/$(id -u) "
            "~/Library/LaunchAgents/com.fulcra.collect.plist`.",
            err=True,
        )
    Daemon().serve()


@cli.command()
def install() -> None:
    """Install the launchd/systemd user agent for the daemon."""
    from . import service_manager
    exe = shutil.which("fulcra-collect") or "fulcra-collect"
    path = service_manager.install(executable=exe)
    click.echo(f"installed service file: {path}")


@cli.command()
def status() -> None:
    """Show every plugin's kind, enabled flag, and last run."""
    try:
        reply = send_request(_socket_path(), {"cmd": "status"})
    except ConnectionError:
        raise click.ClickException(
            "fulcra-collect daemon is not running — start it with "
            "`fulcra-collect daemon` or install it with `fulcra-collect install`."
        )
    for p in reply["plugins"]:
        flag = "on " if p["enabled"] else "off"
        last = p["last_outcome"] or "never run"
        click.echo(f"  [{flag}] {p['id']:<20} {p['kind']:<10} {last}")
    for name, err in reply.get("load_errors", {}).items():
        click.echo(f"  load error: {name}: {err}", err=True)


@cli.command()
@click.argument("plugin_id")
def run(plugin_id: str) -> None:
    """Trigger one run of a plugin now."""
    reply = send_request(_socket_path(), {"cmd": "run", "plugin": plugin_id})
    if not reply.get("ok"):
        raise click.ClickException(reply.get("error", "run failed"))
    click.echo(f"triggered: {plugin_id}")


def _toggle(plugin_id: str, *, on: bool) -> None:
    cfg = config_mod.load()
    cfg.enable(plugin_id) if on else cfg.disable(plugin_id)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass  # daemon not running — config is saved; it'll read it on next start


@cli.command()
@click.argument("plugin_id")
def enable(plugin_id: str) -> None:
    """Enable a plugin."""
    _toggle(plugin_id, on=True)
    click.echo(f"enabled: {plugin_id}")


@cli.command()
@click.argument("plugin_id")
def disable(plugin_id: str) -> None:
    """Disable a plugin."""
    _toggle(plugin_id, on=False)
    click.echo(f"disabled: {plugin_id}")


@cli.command(name="set-interval")
@click.argument("plugin_id")
@click.argument("seconds", type=int)
def set_interval(plugin_id: str, seconds: int) -> None:
    """Override a scheduled plugin's cadence (in seconds)."""
    cfg = config_mod.load()
    cfg.set_interval(plugin_id, seconds)
    config_mod.save(cfg)
    try:
        send_request(_socket_path(), {"cmd": "reload"})
    except ConnectionError:
        pass
    click.echo(f"{plugin_id}: interval set to {seconds}s")


@cli.command(name="set-credential")
@click.argument("plugin_id")
@click.argument("key")
def set_credential(plugin_id: str, key: str) -> None:
    """Store a plugin secret in the OS keychain (prompts, hidden input)."""
    value = click.prompt(f"{plugin_id}/{key}", hide_input=True)
    credentials.set_secret(plugin_id, key, value)
    click.echo(f"stored {plugin_id}/{key}")


@cli.command(name="_worker", hidden=True)
@click.argument("plugin_id")
def _worker(plugin_id: str) -> None:
    """Internal — the worker-subprocess entrypoint."""
    # Same logging setup as the daemon: the worker is a separate process,
    # so without this its INFO records would hit the no-handler last-resort
    # path and vanish — the same gap the daemon fix closes.
    _configure_logging()
    sys.exit(worker.main([plugin_id]))


@cli.command()
def doctor() -> None:
    """Run a pre-flight diagnostic — checks the ten things that most often
    go wrong during first-run onboarding and prints OK / WARN / FAIL for each.

    Exits 0 when there are no FAILs, 1 otherwise. Output is copy/pasteable
    into a bug report or support thread.
    """
    import json
    import platform
    import subprocess

    from . import service_manager
    from .control import send_request as _send_request

    ok_count = warn_count = fail_count = 0

    def _row(label: str, status: str, detail: str) -> None:
        nonlocal ok_count, warn_count, fail_count
        width = 42
        padded = label.ljust(width)
        click.echo(f"  {padded} [{status}]  {detail}")
        if status == "OK":
            ok_count += 1
        elif status == "WARN":
            warn_count += 1
        else:
            fail_count += 1

    click.echo("fulcra-collect doctor")
    click.echo("─" * 66)

    # ── 1. fulcra CLI on PATH ────────────────────────────────────────────────
    cli_path = credentials._find_fulcra_cli()
    if cli_path:
        _row("fulcra CLI on PATH", "OK", cli_path)
    else:
        _row(
            "fulcra CLI on PATH",
            "FAIL",
            "not found — fix: uv tool install fulcra-api",
        )

    # ── 2. fulcra CLI reachable (actually works / signed in) ─────────────────
    signed_in: bool | None = None  # None = couldn't determine (CLI missing/timeout)
    if cli_path:
        try:
            r = subprocess.run(
                [cli_path, "auth", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                signed_in = True
                _row("fulcra CLI reachable", "OK", "auth OK, token returned")
            else:
                signed_in = False
                _row(
                    "fulcra CLI reachable",
                    "WARN",
                    "CLI returned non-zero or empty token — fix: fulcra auth login",
                )
        except subprocess.TimeoutExpired:
            _row(
                "fulcra CLI reachable",
                "FAIL",
                "timed out invoking fulcra auth print-access-token",
            )
    else:
        _row("fulcra CLI reachable", "FAIL", "skipped — CLI not found (see above)")

    # ── 3. fulcra CLI file group present ─────────────────────────────────────
    # `fulcra file --help` exiting 0 proves the CLI build carries the
    # file-commands group. There is no `fulcra --version` to ask, so
    # capability rows are feature probes against subcommand --help.
    _UPGRADE_HINT = "fix: uv tool install --upgrade fulcra-api"
    if cli_path:
        try:
            r = subprocess.run(
                [cli_path, "file", "--help"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                _row("fulcra CLI file group", "OK", "file commands available")
            else:
                _row("fulcra CLI file group", "FAIL",
                     f"`fulcra file --help` exited {r.returncode} — {_UPGRADE_HINT}")
        except subprocess.TimeoutExpired:
            _row("fulcra CLI file group", "FAIL",
                 "timed out invoking fulcra file --help")
    else:
        _row("fulcra CLI file group", "FAIL", "skipped — CLI not found (see above)")

    # ── 4. fulcra CLI data-updates (fulcra-api >= 0.1.35) ────────────────────
    data_updates_available = False
    if cli_path:
        try:
            r = subprocess.run(
                [cli_path, "data-updates", "--help"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                data_updates_available = True
                _row("fulcra CLI data-updates (>=0.1.35)", "OK",
                     "data-updates command available")
            else:
                _row("fulcra CLI data-updates (>=0.1.35)", "FAIL",
                     f"`fulcra data-updates --help` exited {r.returncode} — "
                     f"{_UPGRADE_HINT}")
        except subprocess.TimeoutExpired:
            _row("fulcra CLI data-updates (>=0.1.35)", "FAIL",
                 "timed out invoking fulcra data-updates --help")
    else:
        _row("fulcra CLI data-updates (>=0.1.35)", "FAIL",
             "skipped — CLI not found (see above)")

    # ── 5. Data liveness via data-updates ────────────────────────────────────
    # One authed round trip that proves data is actually flowing. Only the
    # data_types counts are summarised — file_changes can be megabytes of
    # coordination-bus churn and must never be printed or logged.
    if data_updates_available:
        try:
            r = subprocess.run(
                [cli_path, "data-updates", "1 hour"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                try:
                    counts = json.loads(r.stdout).get("data_types") or {}
                    n_records = sum(int(v) for v in counts.values())
                    _row("Fulcra data liveness (last hour)", "OK",
                         f"{len(counts)} data type(s), "
                         f"{n_records} record(s) processed")
                except (json.JSONDecodeError, TypeError, ValueError):
                    _row("Fulcra data liveness (last hour)", "FAIL",
                         "data-updates returned unparseable output")
            elif signed_in is False:
                _row("Fulcra data liveness (last hour)", "WARN",
                     "skipped — not signed in (fix: fulcra auth login; "
                     "see 'fulcra CLI reachable' above)")
            else:
                stderr_tail = (r.stderr or "").strip()[-200:]
                _row("Fulcra data liveness (last hour)", "FAIL",
                     f"data-updates failed: {stderr_tail or 'no stderr'}")
        except subprocess.TimeoutExpired:
            _row("Fulcra data liveness (last hour)", "FAIL",
                 "timed out invoking fulcra data-updates")
    else:
        _row("Fulcra data liveness (last hour)", "FAIL",
             "skipped — data-updates unavailable (see above)")

    # ── 6. Daemon control socket reachable ───────────────────────────────────
    sock = config_mod.config_dir() / "control.sock"
    plist_exists = (
        service_manager.launchd_plist_path().exists()
        if platform.system() == "Darwin"
        else service_manager.systemd_unit_path().exists()
        if platform.system() == "Linux"
        else False
    )
    try:
        reply = _send_request(sock, {"cmd": "status"}, timeout=3.0)
        if reply.get("ok") is not False:
            n = len(reply.get("plugins", []))
            _row("Daemon control socket", "OK", f"reachable, {n} plugin(s) loaded")
        else:
            _row("Daemon control socket", "FAIL", f"daemon replied with error: {reply.get('error', '?')}")
    except (ConnectionError, OSError, TimeoutError):
        if plist_exists:
            _row(
                "Daemon control socket",
                "FAIL",
                "not reachable — fix: launchctl bootstrap "
                f"gui/$(id -u) {service_manager.launchd_plist_path()}",
            )
        else:
            _row(
                "Daemon control socket",
                "FAIL",
                "not reachable — fix: uv run fulcra-collect install  (then bootstrap)",
            )

    # ── 7. launchd / systemd agent installed ─────────────────────────────────
    system = platform.system()
    if system == "Darwin":
        plist_path = service_manager.launchd_plist_path()
        if plist_path.exists():
            _row("launchd agent installed", "OK", str(plist_path))
        else:
            _row(
                "launchd agent installed",
                "WARN",
                "plist not found — fix: uv run fulcra-collect install",
            )
    elif system == "Linux":
        unit_path = service_manager.systemd_unit_path()
        if unit_path.exists():
            _row("systemd unit installed", "OK", str(unit_path))
        else:
            _row(
                "systemd unit installed",
                "WARN",
                "unit not found — fix: uv run fulcra-collect install",
            )
    else:
        _row("Service agent installed", "OK", f"skipped (platform={system!r})")

    # ── 8. launchd agent loaded + running (macOS only) ───────────────────────
    if system == "Darwin":
        try:
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{service_manager.LAUNCHD_LABEL}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                _row("launchd agent running", "OK", "agent is loaded and running")
            else:
                plist = service_manager.launchd_plist_path()
                _row(
                    "launchd agent running",
                    "WARN",
                    f"installed but not loaded — fix: launchctl bootstrap "
                    f"gui/$(id -u) {plist}",
                )
        except subprocess.TimeoutExpired:
            _row("launchd agent running", "WARN", "launchctl timed out")
        except FileNotFoundError:
            _row("launchd agent running", "WARN", "launchctl not found (non-macOS?)")

    # ── 9. Web token + cookie bootstrap ─────────────────────────────────────
    web_token_path = config_mod.config_dir() / "web-token"
    if web_token_path.exists() and web_token_path.read_bytes().strip():
        _row("Web token present", "OK", str(web_token_path))
    else:
        _row(
            "Web token present",
            "FAIL",
            "~/.config/fulcra-collect/web-token absent — fix: start the daemon "
            "(it writes this file on startup)",
        )

    # ── 10. Bearer token in keychain + Fulcra API healthy ────────────────────
    tok = credentials.get_user_secret("bearer-token")
    if not tok:
        web_url_path = config_mod.config_dir() / "web-url"
        url_hint = (
            web_url_path.read_text().strip()
            if web_url_path.exists()
            else "http://127.0.0.1:9292"
        )
        _row(
            "Bearer token / API health",
            "WARN",
            f"not signed in yet — fix: open {url_hint}",
        )
    else:
        try:
            import httpx

            resp = httpx.get(
                "https://api.fulcradynamics.com/user/v1alpha1/annotation",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            if resp.status_code == 200:
                _row("Bearer token / API health", "OK", "API responded 200")
            elif resp.status_code == 401:
                _row(
                    "Bearer token / API health",
                    "WARN",
                    "token rejected (401) — fix: Reconnect via Settings in the web UI",
                )
            else:
                _row(
                    "Bearer token / API health",
                    "WARN",
                    f"unexpected API status {resp.status_code}",
                )
        except Exception as exc:  # noqa: BLE001 — network errors, httpx not installed
            _row(
                "Bearer token / API health",
                "FAIL",
                f"network error / timeout: {exc}",
            )

    click.echo("─" * 66)
    click.echo(
        f"  {ok_count} passed, {warn_count} warning(s), {fail_count} failed"
    )
    if fail_count:
        raise SystemExit(1)


@cli.group()
def plugin() -> None:
    """Per-plugin state and configuration commands."""


@plugin.command("reset-definition")
@click.argument("plugin_id")
def reset_definition(plugin_id: str) -> None:
    """Clear the cached Fulcra definition id for a plugin so the next
    run re-resolves (and possibly adopts a different definition)."""
    from . import state
    st = state.load(plugin_id)
    st.definition_id = None
    state.save(st)
    click.echo(f"Cleared definition_id cache for {plugin_id!r}.")
