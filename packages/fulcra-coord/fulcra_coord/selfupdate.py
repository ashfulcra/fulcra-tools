"""Version self-incorporation: keep the installed CLI current from a bus pointer.

WHY this exists (operator directive, 2026-06-10): "i'm not going to go around
and wake the entire fleet for each incremental upgrade." Every release used to
need a manual "UPDATE NOW" broadcast plus per-host hand-holding; hosts that
missed it silently froze on an old subcommand set. Now the maintainer publishes
a tiny version manifest (``runtime/version.json``, via ``announce-version``)
and every session-start (``connect``) and listener tick (``notify-inbox``)
checks it and updates itself — DEFAULT ON (operator call 2026-06-10,
superseding the spec's opt-in note), env opt-out ``FULCRA_COORD_SELF_UPDATE=0``.

THE SAFETY BOUNDARY (the reconciled spec's non-negotiable rail,
docs/superpowers/specs/2026-06-08-greenfield-reconciled.md — auto-update is a
supply-chain risk on a public multi-account repo, so):

  * **The bus carries a POINTER, never a payload.** The manifest is version
    string + commit sha + min_supported and NOTHING else; its validator
    rejects extra keys, so it cannot even smuggle a command. A malformed or
    tampered manifest reads as "never behind" — a bad bus record can never
    trigger an update.
  * **The update argv is built from LOCAL config only.** Either an explicit
    ``update-cmd.json`` ``{"cmd": [...], "cwd": ...}`` or the built-in default
    derived from ``update.json`` ``{"checkout": "/path/to/fulcra-tools"}``:
    ``git -C <checkout> pull --ff-only`` then ``uv tool install --reinstall
    --force <checkout>/packages/fulcra-coord``. The agent updates the KNOWN
    package from the operator's OWN configured checkout (the trusted source);
    nothing read off the bus ever reaches an exec boundary.
  * **Degrade gracefully, visibly, never break.** No config / a failed update
    -> a one-time WARN + a local stale marker that ``connect`` renders as
    ``(vX behind canonical Y)`` in the presence summary, so staleness is
    VISIBLE on the roster instead of silently rotting. Both call sites are
    fully best-effort: ``maybe_self_update`` never raises.

A successful update takes effect on the NEXT invocation — this process keeps
running its already-imported code (deliberately: re-exec'ing mid-session is
the kind of cleverness that corrupts session state).

stdlib-only leaf over cache/remote/schema — never imports cli/presence/inbox.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Bound into THIS namespace (the wake.Popen pattern) so tests can patch
# fulcra_coord.selfupdate._run_proc without mocking subprocess.run for every
# other module that shares the global subprocess import.
from subprocess import DEVNULL, run as _run_proc

from . import __version__, cache, env_float, remote, schema

# Hard ceiling on each update step (git pull / uv tool install). Generous —
# a cold uv build can take a while — but bounded, so a hung network can never
# wedge a session boot or a listener tick forever.
UPDATE_TIMEOUT_S = 300

# Tick-path throttle default: at most one manifest check per 6 hours
# (FULCRA_COORD_SELF_UPDATE_INTERVAL_H overrides). connect is NOT throttled —
# a fresh session should never boot stale because a tick checked recently.
CHECK_INTERVAL_H_DEFAULT = 6.0


# ---------------------------------------------------------------------------
# The pure check
# ---------------------------------------------------------------------------

def _version_tuple(v: Any) -> Optional[tuple[int, ...]]:
    """Parse '0.15.2'-shaped versions to an int tuple for comparison.

    Deliberately a tiny vendored PEP440-ISH compare, not a packaging dependency
    (the rails say: no new deps for this). Each dot segment contributes its
    leading integer ('0', '15', '2', and '0rc1' -> 0 — pre-release tags compare
    equal to their release, an acceptable simplification for a fleet that ships
    plain x.y.z). Any segment with no leading digits -> None (malformed)."""
    if not isinstance(v, str) or not v.strip():
        return None
    parts: list[int] = []
    for seg in v.strip().split("."):
        m = re.match(r"(\d+)", seg)
        if not m:
            return None
        parts.append(int(m.group(1)))
    return tuple(parts)


def is_behind(installed: str, manifest: Any) -> bool:
    """Is ``installed`` strictly behind the manifest's canonical version?

    PURE (no I/O) and fail-CLOSED on garbage: an invalid manifest (including
    one carrying extra keys — the pointer-rule violation) or an unparseable
    version on EITHER side reads as "not behind", so nothing downstream can
    ever update off a record we don't fully understand. Tuples are zero-padded
    so '0.15' vs '0.15.0' compare equal."""
    if schema.validate_version_manifest(manifest):
        return False
    mine = _version_tuple(installed)
    theirs = _version_tuple(manifest["package_version"])
    if mine is None or theirs is None:
        return False
    width = max(len(mine), len(theirs))
    pad = lambda t: t + (0,) * (width - len(t))  # noqa: E731
    return pad(mine) < pad(theirs)


def _valid_manifest_not_behind(installed: str, manifest: Any) -> bool:
    """True only when a trusted manifest proves this install is current/ahead.

    ``is_behind`` intentionally returns False for malformed/absent manifests so
    garbage can never trigger an update. That fail-closed False is not proof the
    host is current, though: clearing an existing stale marker on garbage would
    hide a known-behind host from the roster during a manifest outage.
    """
    if schema.validate_version_manifest(manifest):
        return False
    mine = _version_tuple(installed)
    theirs = _version_tuple(manifest["package_version"])
    if mine is None or theirs is None:
        return False
    width = max(len(mine), len(theirs))
    pad = lambda t: t + (0,) * (width - len(t))  # noqa: E731
    return pad(mine) >= pad(theirs)


# ---------------------------------------------------------------------------
# Local state: config, markers, log
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "fulcra-coord"


def _load_json_config(name: str) -> dict:
    """Best-effort load of one optional config file: ANY problem -> {} (the
    wake.json loader contract — a corrupt config degrades to 'not configured',
    never breaks a session boot or a tick)."""
    try:
        p = _config_dir() / name
        if not p.is_file():
            return {}
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_update_plan() -> Optional[
        tuple[list[list[str]], Optional[str], Optional[str], Optional[str]]]:
    """Resolve (argv list, cwd, checkout, branch) for the update — from LOCAL
    config ONLY.

    Precedence: an explicit ``update-cmd.json`` {"cmd": [...], "cwd": ...}
    wins (full operator control — checkout/branch are None: the operator's
    own command owns its safety); else ``update.json`` {"checkout": ...,
    "branch": ...} yields the built-in safe default with both argvs BUILT IN
    CODE from the configured path — the pointer rule's exec-side half: no
    string from the bus is ever part of an argv. ``branch`` (default
    ``main``) is the canonical branch the S1 guard requires the checkout to
    actually be on before any pull (2026-06-11 bug hunt). No (valid) config
    -> None (the caller degrades visibly). There is deliberately NO default
    checkout path: the package cannot reliably know where the operator's
    canonical clone lives, and guessing wrong would `git pull` someone
    else's directory."""
    cmd_cfg = _load_json_config("update-cmd.json")
    cmd = cmd_cfg.get("cmd")
    if (isinstance(cmd, list) and cmd
            and all(isinstance(t, str) and t for t in cmd)):
        cwd = cmd_cfg.get("cwd")
        return ([list(cmd)],
                (cwd if isinstance(cwd, str) and cwd else None), None, None)

    co_cfg = _load_json_config("update.json")
    checkout = co_cfg.get("checkout")
    if isinstance(checkout, str) and checkout and Path(checkout).is_dir():
        branch = co_cfg.get("branch")
        branch = branch if isinstance(branch, str) and branch.strip() else "main"
        return ([
            ["git", "-C", checkout, "pull", "--ff-only"],
            ["uv", "tool", "install", "--reinstall", "--force",
             f"{checkout}/packages/fulcra-coord"],
        ], None, checkout, branch.strip())
    return None


def _checkout_branch(checkout: str) -> Optional[str]:
    """Current branch of the configured checkout, or None when undeterminable
    (no git, not a repo, detached HEAD reads as ``HEAD``, probe error).

    2026-06-11 bug hunt S1 (a): the updater used to ff-pull WHATEVER branch
    the checkout happened to be on — an operator mid-feature-work would have
    their feature branch pulled against its upstream (noisy failure at best,
    a surprise fast-forward at worst). The caller refuses to pull unless this
    equals the configured canonical branch; None fails CLOSED (a pull into an
    unknowable working-tree state is a blind mutation)."""
    try:
        result = _run_proc(["git", "-C", checkout, "rev-parse",
                            "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return (result.stdout or "").strip() or None
    except Exception:
        pass
    return None


def _update_log_path() -> Path:
    """Append-target for update-command output, in the local cache dir — the
    breadcrumb when an unattended update misbehaves."""
    return cache.cache_root() / "self-update.log"


def _stale_marker_path() -> Path:
    """Local marker: this host is behind canonical and could not update.
    Written by maybe_self_update, rendered by connect as the roster suffix —
    the 'degraded but VISIBLE' half of the safety boundary."""
    return cache.cache_root() / "self-update-stale.json"


def _warned_marker_path() -> Path:
    return cache.cache_root() / "self-update-warned"


def _throttle_marker_path() -> Path:
    """Tick-path throttle marker (mtime = last manifest check)."""
    return cache.cache_root() / "self-update-checked"


def _write_stale_marker(installed: str, canonical: str) -> None:
    try:
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        _stale_marker_path().write_text(
            json.dumps({"installed": installed, "canonical": canonical}))
    except Exception:
        pass


def _clear_stale_marker() -> None:
    try:
        _stale_marker_path().unlink(missing_ok=True)
        # Re-arm the one-time no-config warn for the NEXT time we fall behind:
        # the message should fire once per degradation episode, not once ever.
        _warned_marker_path().unlink(missing_ok=True)
    except Exception:
        pass


def _clear_attempt_marker() -> None:
    """Drop the per-canonical attempt throttle (S1 (c)) — called only once a
    trusted manifest proves the host CURRENT. Deliberately NOT folded into
    _clear_stale_marker: the 'updated' path clears the stale marker on the
    updater's claim of success, but the attempt marker must survive until
    the version comparison actually confirms the update took (that survival
    is the whole ineffective-update guard)."""
    try:
        _attempt_marker_path().unlink(missing_ok=True)
    except Exception:
        pass


def stale_summary_suffix() -> str:
    """The roster-visible staleness suffix, or '' when current/unknown.

    connect appends this to the presence summary so the OPERATOR sees
    '(v0.15.2 behind canonical 0.16.0)' on the roster — the chosen degraded
    marker (simpler and more visible than a separate presence field, and old
    readers render it for free since it is just summary text)."""
    try:
        data = json.loads(_stale_marker_path().read_text())
        installed, canonical = data.get("installed"), data.get("canonical")
        if installed and canonical:
            return f"(v{installed} behind canonical {canonical})"
    except Exception:
        pass
    return ""


def _warn_no_config_once() -> None:
    """One warn per degradation episode (marker-deduped): self-update is ON
    by default, so a host with no update config would otherwise nag on every
    tick forever. Cleared alongside the stale marker once current again."""
    try:
        marker = _warned_marker_path()
        if marker.exists():
            return
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        marker.write_text("")
    except Exception:
        pass
    print("[fulcra-coord] self-update enabled but no update.json/update-cmd.json "
          "configured — this host will fall behind canonical releases. See "
          "README 'Self-update'.", file=sys.stderr)


def _update_lock_path() -> Path:
    """Local mutual-exclusion lock around the update run (S1 (b))."""
    return cache.cache_root() / "self-update.lock"


def _acquire_update_lock() -> bool:
    """Take the local update lock via O_EXCL create; False when held.

    2026-06-11 bug hunt S1 (b): connect (unthrottled) and a listener tick can
    race into ``_run_update`` concurrently — two ``git pull`` + ``uv tool
    install`` runs over the SAME checkout interleaving is exactly the kind of
    mess a half-written venv comes from. O_EXCL on the local filesystem is
    the correct scope (both contenders are processes on this host).

    Stale-break: a lock older than UPDATE_TIMEOUT_S can only be a crashed
    holder — every update step is bounded by that timeout — so it is removed
    and re-taken (still via O_EXCL, so two breakers can't both win)."""
    import time
    path = _update_lock_path()

    def _try_create() -> bool:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True

    try:
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        try:
            return _try_create()
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0.0
            if age <= UPDATE_TIMEOUT_S:
                return False   # live holder — skip, never steal
            try:
                path.unlink()
            except OSError:
                pass
            try:
                return _try_create()
            except FileExistsError:
                return False   # another breaker won the re-take race
    except Exception:
        # Lock machinery failure must not wedge self-update forever; the
        # worst case without the lock is the pre-fix behavior.
        return True


def _release_update_lock() -> None:
    try:
        _update_lock_path().unlink(missing_ok=True)
    except Exception:
        pass


def _attempt_marker_path() -> Path:
    """Throttle marker for UPDATE ATTEMPTS (distinct from the tick's
    manifest-CHECK throttle): mtime = last attempt, body = the canonical
    version that attempt targeted."""
    return cache.cache_root() / "self-update-attempted.json"


def _arm_attempt_marker(canonical: str) -> None:
    try:
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        _attempt_marker_path().write_text(json.dumps({"canonical": canonical}))
    except Exception:
        pass


def _attempt_due(canonical: str) -> bool:
    """May we attempt an update toward ``canonical`` right now?

    2026-06-11 bug hunt S1 (c): connect is deliberately UNthrottled on the
    manifest CHECK (a fresh session must never boot stale), but that meant a
    failing — or 'successful' but ineffective — update re-ran the whole
    git+uv pipeline on EVERY session start, forever. After any attempt that
    leaves __version__ behind, further attempts toward the SAME canonical
    version are skipped for the tick interval (the same 6h /
    FULCRA_COORD_SELF_UPDATE_INTERVAL_H knob). Keyed per-canonical so a NEW
    release gets one immediate attempt even inside the window — the operator
    may have just shipped the fix for the broken updater itself."""
    try:
        path = _attempt_marker_path()
        data = json.loads(path.read_text())
        if data.get("canonical") != canonical:
            return True
        import time
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        return age_h >= env_float("FULCRA_COORD_SELF_UPDATE_INTERVAL_H",
                                  CHECK_INTERVAL_H_DEFAULT)
    except Exception:
        return True   # no/garbled marker -> first attempt


def _throttle_due() -> bool:
    try:
        import time
        age_h = (time.time() - _throttle_marker_path().stat().st_mtime) / 3600.0
        return age_h >= env_float("FULCRA_COORD_SELF_UPDATE_INTERVAL_H",
                                  CHECK_INTERVAL_H_DEFAULT)
    except OSError:
        return True  # no marker yet -> first check


def _touch_throttle_marker() -> None:
    try:
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        _throttle_marker_path().write_text("")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The I/O orchestration
# ---------------------------------------------------------------------------

def _download_manifest(backend: Optional[list[str]] = None) -> Any:
    """Direct download of the canonical manifest — deliberately never a view
    (views are rebuildable caches that can lag; the version pointer must be
    read from its authoritative file)."""
    return remote.download_json(remote.version_manifest_path(), backend=backend)


def _run_update(argvs: list[list[str]], cwd: Optional[str]) -> bool:
    """Run the resolved update argv(s) sequentially, bounded, output to the
    cache-dir log. True iff every step exited 0. Never raises (callers are a
    session boot and a polling tick). Callers hold the update lock (S1 (b))."""
    log = _update_log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            for argv in argvs:
                fh.write(f"\n[self-update] $ {' '.join(argv)}\n".encode())
                fh.flush()
                result = _run_proc(argv, cwd=cwd, stdin=DEVNULL, stdout=fh,
                                   stderr=fh, timeout=UPDATE_TIMEOUT_S)
                if result.returncode != 0:
                    return False
        return True
    except Exception:
        return False


def maybe_self_update(*, backend: Optional[list[str]] = None,
                      throttle: bool = False) -> str:
    """Check the bus version pointer and self-update if behind. NEVER raises.

    DEFAULT ON; ``FULCRA_COORD_SELF_UPDATE=0`` opts out (the operator's
    2026-06-10 call, superseding the spec's opt-in note — "i'm not going to go
    around and wake the entire fleet for each incremental upgrade").
    ``throttle=True`` is the listener-tick mode: at most one check per
    FULCRA_COORD_SELF_UPDATE_INTERVAL_H (default 6h, mtime marker); connect
    passes False so a fresh session always checks.

    Returns a status token (for logs/tests): disabled | throttled | current |
    updated | degraded-no-config | wrong-branch | attempt-throttled | locked |
    update-failed | error. A successful update does NOT re-exec — this process
    logs 'takes effect next invocation' and continues on its already-imported
    code; the next wake/session runs new.

    2026-06-11 bug hunt S1 guards on the update path (all degrade visibly via
    the stale marker, never break a boot/tick):
      * wrong-branch — the configured checkout is not on its canonical branch
        (update.json "branch", default main): refuse to ff-pull it.
      * attempt-throttled — a recent attempt toward the SAME canonical
        version didn't take; don't re-run git+uv every session start.
      * locked — another process on this host is mid-update right now."""
    try:
        if (os.environ.get("FULCRA_COORD_SELF_UPDATE") or "").strip() == "0":
            return "disabled"
        if throttle:
            if not _throttle_due():
                return "throttled"
            # Arm BEFORE the check so a failing remote can't make every tick
            # re-pay the manifest round-trip for 6 hours straight.
            _touch_throttle_marker()
        manifest = _download_manifest(backend)
        if not is_behind(__version__, manifest):
            # Valid current/ahead manifest: drop any stale marker so the roster
            # suffix heals itself. Invalid/absent manifest is fail-closed (no
            # update) but NOT proof of freshness, so preserve any existing
            # stale marker rather than making a known-behind host invisible.
            if _valid_manifest_not_behind(__version__, manifest):
                _clear_stale_marker()
                _clear_attempt_marker()
            return "current"
        canonical = manifest["package_version"]
        plan = _resolve_update_plan()
        if plan is None:
            _warn_no_config_once()
            _write_stale_marker(__version__, canonical)
            return "degraded-no-config"
        argvs, cwd, checkout, branch = plan
        # S1 (a): the built-in checkout plan must never pull a branch other
        # than the configured canonical one (operator mid-feature-work parks
        # checkouts on feature branches; a blind ff-pull there is either a
        # noisy permanent failure or a surprise fast-forward). update-cmd.json
        # plans skip this — the operator's own command owns its safety.
        if checkout is not None:
            actual = _checkout_branch(checkout)
            if actual != branch:
                _write_stale_marker(__version__, canonical)
                print(f"[fulcra-coord] self-update refused: checkout "
                      f"{checkout} is on branch {actual or 'unknown'!r}, not "
                      f"the configured {branch!r} — staying on {__version__}",
                      file=sys.stderr)
                return "wrong-branch"
        # S1 (c): connect's manifest check stays unthrottled, but ATTEMPTS
        # toward a canonical version that a recent attempt failed to reach
        # are — otherwise every session start re-runs the whole git+uv
        # pipeline while an update keeps failing or not taking effect.
        if not _attempt_due(canonical):
            _write_stale_marker(__version__, canonical)
            return "attempt-throttled"
        # S1 (b): one update at a time per host — connect and a listener tick
        # racing into git/uv over the same checkout must not interleave.
        if not _acquire_update_lock():
            _write_stale_marker(__version__, canonical)
            print(f"[fulcra-coord] self-update to {canonical} skipped: "
                  f"another update is in progress on this host",
                  file=sys.stderr)
            return "locked"
        try:
            updated = _run_update(argvs, cwd)
        finally:
            _release_update_lock()
        # Arm the per-canonical attempt throttle regardless of outcome: a
        # genuinely effective update reads "current" next time (which clears
        # it); a failed/ineffective one must not retry every session start.
        _arm_attempt_marker(canonical)
        if updated:
            _clear_stale_marker()
            print(f"[fulcra-coord] self-update: updated to {canonical} — "
                  f"takes effect next invocation", file=sys.stderr)
            return "updated"
        _write_stale_marker(__version__, canonical)
        print(f"[fulcra-coord] self-update to {canonical} FAILED — staying on "
              f"{__version__}; see {_update_log_path()}", file=sys.stderr)
        return "update-failed"
    except Exception as e:  # the fail-safe contract for both call sites
        try:
            print(f"[fulcra-coord] self-update check errored (non-fatal): {e}",
                  file=sys.stderr)
        except Exception:
            pass
        return "error"


# ---------------------------------------------------------------------------
# announce-version — the maintainer's release-time publish
# ---------------------------------------------------------------------------

def _local_release_commit() -> str:
    """Best-effort ``git rev-parse HEAD`` of the announcing host's cwd —
    provenance only (nothing consumes it programmatically), so any failure
    (no git, not a checkout) degrades to ''."""
    try:
        result = _run_proc(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def cmd_announce_version(args: Any, backend: Optional[list[str]] = None) -> int:
    """Publish the canonical version manifest (maintainer runs at each release).

    Reads the INSTALLED ``__version__`` (the single source of truth pyproject
    derives from) — announcing exactly what this build IS, never a typed-in
    version that could drift. Verify-after-write (the writepipe post-stat
    pattern): the upload only counts once a stat confirms the record landed,
    because a silently-missing manifest would freeze the whole fleet's
    self-update without any error anywhere."""
    manifest = schema.make_version_manifest(
        __version__,
        _local_release_commit(),
        min_supported=getattr(args, "min_supported", None),
    )
    errors = schema.validate_version_manifest(manifest)
    if errors:  # can't happen via make_version_manifest; belt-and-braces
        print(f"ERROR: refusing to publish invalid manifest: {errors}",
              file=sys.stderr)
        return 1
    path = remote.version_manifest_path()
    if not remote.upload_json(manifest, path, backend=backend):
        print(f"ERROR: manifest upload failed ({path})", file=sys.stderr)
        return 1
    if remote.stat(path, backend=backend) is None:
        print(f"ERROR: manifest upload not verifiable ({path}) — the fleet "
              f"would silently stop updating; re-run announce-version",
              file=sys.stderr)
        return 1
    if getattr(args, "format", "table") == "json":
        print(json.dumps(manifest, indent=2))
    else:
        print(f"Announced fulcra-coord {manifest['package_version']} "
              f"(commit {manifest['release_commit'] or 'unknown'}) -> {path}")
    return 0
