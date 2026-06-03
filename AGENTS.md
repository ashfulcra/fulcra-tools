# Fulcra Collect — agent guide

uv-workspace monorepo, macOS-first. Packages under `packages/`:
- `collect` — the daemon (control socket + FastAPI web onboarding wizard) and plugins
- `menubar` — the macOS menu-bar app (PyObjC / rumps)
- `fulcra-common` — shared code; plus importer packages (dayone, csv-importer, media-helpers, attention…)

## Setup & tests
- Full install: **`uv sync --all-packages --all-extras`**. Bare `uv sync` is NOT enough — pytest lives in each package's `dev` extra and PyObjC/rumps in the `macos` extra, so a bare sync fails tests with `Failed to spawn: pytest` and the menu-bar can't import.
- Run tests: `uv run pytest packages/ -q` (~1500 tests, ~20s, and must NOT hit the network — a network-bound run is the bug, not slowness).
- Editable install: the `.venv` imports the live workspace source, so a code change is picked up by **restarting the daemon**, not re-syncing.
- Pull latest into a checkout with `bash scripts/update.sh` (git pull + `uv sync --all-packages --all-extras` + restart daemon/menubar). Any sync must keep `--all-extras` or it prunes pytest + PyObjC back out.
- PyObjC-free logic is split into its own modules so tests run on Linux CI; macOS view-layer tests are marked and skipped off-darwin. Keep new PyObjC imports lazy (inside functions), never at module import time.

## The daemon
- Run it durably as a **launchd** agent, NOT a backgrounded shell process — a foreground/`&` daemon dies when its terminal or session ends. Install + load:
  `uv run fulcra-collect install` then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fulcra.collect.plist`.
  Restart: `launchctl kickstart -k gui/$(id -u)/com.fulcra.collect`. Stop: `launchctl bootout gui/$(id -u)/com.fulcra.collect`. Logs: `~/Library/Logs/fulcra-collect/`.
- Subcommands: `daemon install status run enable disable set-credential set-interval plugin`. There is **no `start`**.
- Config dir `~/.config/fulcra-collect/`: `control.sock` (UDS the menu-bar + CLI use), `web-url` (default `http://127.0.0.1:9292`), `web-token` (Bearer for the web API).

## launchd PATH gotcha
launchd runs the daemon with a restricted PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) and does NOT source your shell profile — so `~/.local/bin` (where `uv tool install fulcra-api` puts the `fulcra` CLI) is invisible. Any code shelling out to the `fulcra` CLI must resolve it via `credentials._find_fulcra_cli()` (PATH → `~/.local/bin` → homebrew), **never** bare `shutil.which("fulcra")`.

## Keychain
- User secrets (the Fulcra `bearer-token`) live in the OS keychain via `keyring`, service `fulcra-collect:user`. A read can block on a macOS ACL confirmation dialog; `credentials._keyring_get` times out after 5s and the daemon degrades to "Fulcra not authenticated".
- Sign in **through the daemon's web wizard** (`open "$(cat ~/.config/fulcra-collect/web-url)"`) so the daemon — not a one-off script — owns the keychain item. If the "Python wants to use your confidential information" prompt repeats, click **Always Allow** (not "Allow"). If it still repeats, the item is owned by a stale binary: `security delete-generic-password -s "fulcra-collect:user" -a "bearer-token"`, restart the daemon, re-sign-in.

## Menu-bar app
- Launch from a GUI (Aqua) session: `uv run --package fulcra-menubar python -m fulcra_menubar`. Not from SSH/detached shells, or the status item won't appear. Under Homebrew Python the bundle id is `org.python.python` (use that for computer-use / TCC grants, not `com.apple.python3`).
- It talks ONLY to the daemon over the control socket; it never reads the keychain. Auth state, tracks, and plugin status all come from the daemon — a stale UI usually just needs a relaunch / reopened popover.
- Bundle-requiring macOS APIs (`UNUserNotificationCenter`, etc.) raise an **uncatchable** NSException when run unbundled (`python -m` from a venv) — `try/except` can't recover it. Guard with `_notify_macos.running_in_app_bundle()`. The shipped app is bundled via Briefcase.

## Sign-in & first run
Full first-run walkthrough + troubleshooting: `docs/TESTING.md`.

## Code review & merge (all repos)
**No direct pushes to `main` — every change goes through a PR, is reviewed by
another agent, and is merged by its original author (not the reviewer).** Open a
PR → `tell` your reviewer "Review PR #n in <repo> — assume there are bugs to
fix" → reviewer commits fixes onto the branch and pings you → you review + merge.
Reviewer routing: non-Arc Claude agents → `codex:Mac.localdomain:main`; Arc
sessions → `claude-code:ArcBot:Arc-Code-Review`. No reviewer response → ping the
operator (`fulcra-coord block --on-user`); never merge unreviewed. Full rule:
`packages/fulcra-coord/adapters/claude-code/CLAUDE.md`.

## Repo homes
This monorepo (Fulcra-internal for now) is **only for things that make Fulcra
useful for other people**. Personal/unrelated projects go in separate repos
under `ashfulcra` or `reversity` — ask the operator when unsure.
