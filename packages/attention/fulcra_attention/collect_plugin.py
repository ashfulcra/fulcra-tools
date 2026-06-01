"""fulcra-collect plugin: bookkeeping for the browser-extension ingest path.

The browser extension used to POST events to a standalone HTTP relay on
127.0.0.1:8771 that this plugin ran as a supervised service. That relay
is gone — the daemon now hosts a `POST /api/extension/attention` route
on its own stable port (default 9292), and the extension POSTs there
directly. See packages/collect/fulcra_collect/web.py.

This plugin now exists for two reasons:
1. It still owns the Attention definition / canonical_definition_name —
   the daemon's definition-resolver wizard step needs a Plugin row to
   bind to.
2. It exposes a `manual` run() that the user can click in the UI to run
   a sanity check (token configured? definition bound? extension has
   posted recently?). It does not produce annotations; the extension
   does that via the daemon route.
"""
from __future__ import annotations

from fulcra_collect.plugin import (
    Credential, Plugin, RunContext, SetupStep,
)

from .definition_spec import attention_resolver_spec
from .state import DEFAULT_PATH
from .state import load as _state_load
from .state import save as _state_save

# The Fulcra annotation definition shape for the Attention DurationAnnotation.
# Passed to ctx.resolved_definition_id as the expected_spec so the shared
# resolver can verify an adopted definition has the right structure, or create
# a new one when none exists.
#
# DERIVED, not hand-maintained: this is the canonical create payload (built by
# the same wire.duration_definition_payload the CLI bootstrap path uses)
# projected onto exactly the keys _spec_matches compares (annotation_type +
# measurement_spec). Single-sourcing it in definition_spec.py means the
# resolver's match-spec can't silently drift from the CLI create payload's
# measurement structure. See fulcra_attention/definition_spec.py.
ATTENTION_SPEC: dict = attention_resolver_spec()


def load_state():
    return _state_load(DEFAULT_PATH)


def run(ctx: RunContext) -> None:
    """Sanity-check the attention pipeline without producing events.

    The browser extension itself produces events via the daemon's
    `/api/extension/attention` route. This callable verifies the user
    has finished setup so the UI can show a green check (or red flag)
    instead of nothing.

    Reports three checks via ctx.progress():
    - extension-token is in the user-level keychain
    - attention definition is bound in this plugin's state
    - state.watermarks has at least one entry within the last 24h
      (i.e. the extension has actually posted something recently)
    """
    from datetime import datetime, timedelta, timezone

    # Check 1: extension-token in keychain
    from fulcra_collect import credentials as _creds
    has_token = _creds.has_user_secret("extension-token")
    ctx.progress(
        check="extension_token",
        ok=has_token,
        detail=(
            "extension-token is set" if has_token
            else "extension-token is missing — pair the extension in the wizard"
        ),
    )

    # Check 2: attention definition bound
    state = load_state()
    if not state.attention_definition_id:
        # Try the shared resolver — it'll adopt an existing "Attention"
        # definition on the account, or create one. Keeps the multi-machine
        # dedup guarantee that bootstrap used to provide.
        try:
            def_id = ctx.resolved_definition_id(
                ATTENTION_SPEC,
                canonical_name="Attention",
            )
            state.attention_definition_id = def_id
            _state_save(state)
            ctx.progress(check="definition_bound", ok=True,
                         detail=f"resolved definition {def_id}")
        except Exception as exc:
            ctx.progress(
                check="definition_bound", ok=False,
                detail=f"could not resolve definition: {exc}",
            )
            return
    else:
        ctx.progress(
            check="definition_bound", ok=True,
            detail=f"definition bound: {state.attention_definition_id}",
        )

    # Check 3: extension has posted recently. We look at state.watermarks —
    # the daemon's extension route updates them on every successful ingest.
    # A watermark within the last 24h means "the extension is alive and
    # connected" from this machine's perspective.
    recent_cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat().replace("+00:00", "Z")
    recent_clients = [
        c for c, w in state.watermarks.items() if w >= recent_cutoff
    ]
    if recent_clients:
        ctx.progress(
            check="recent_activity", ok=True,
            detail=f"recent posts from: {', '.join(recent_clients)}",
        )
    else:
        ctx.progress(
            check="recent_activity", ok=False,
            detail=(
                "no posts in the last 24h — open a tab in the browser "
                "extension and check it's pointed at the daemon"
            ),
        )


PLUGIN = Plugin(
    id="attention-relay",
    name="Attention",
    # kind="manual" but collect_mode="live_continuous" — see SP3 mapping
    # table. The run() callable is a no-op status check; the actual data
    # flow is push-based from the browser extension to the daemon's
    # webhook endpoint, which is functionally live_continuous despite the
    # technical "manual" kind. This is the only plugin where collect_mode
    # is NOT derivable from kind, and the explicit per-plugin declaration
    # exists precisely so cases like this can be modelled correctly.
    kind="manual",
    collect_mode="live_continuous",
    run=run,
    description=(
        "Receives browser activity from the Fulcra Attention extension "
        "(which tabs you have open, when you're idle in the browser) and "
        "forwards it to Fulcra. The browser extension posts events directly "
        "to the Fulcra Collect daemon's HTTP endpoint; this plugin owns the "
        "Attention annotation definition and verifies the pipeline is "
        "wired up."
    ),
    category="activity",
    canonical_definition_name="Attention",
    required_credentials=(
        Credential(
            key="extension-token",
            label="Extension token",
            # Account-scoped, not plugin-scoped: the pair route writes it via
            # credentials.set_user_secret("extension-token", ...), the ingest
            # route reads it via get_user_secret, and run() above checks it
            # via has_user_secret. user_level=True keeps the credential-status
            # endpoint reading from that same "fulcra-collect:user" store so it
            # no longer reports a working token as "missing".
            user_level=True,
            help=(
                "Shared secret the browser extension uses to authenticate "
                "to the daemon. The pairing step in setup generates and "
                "installs this automatically; manual entry is only needed "
                "if the one-click handshake doesn't reach the extension."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="How Attention works",
            body_md=(
                "Attention captures what you're doing in your browser — which "
                "tabs you have open, which one is active, when you're idle. "
                "It runs in two pieces:\n\n"
                "- A **browser extension** that watches your tabs and posts "
                "events to the Fulcra Collect daemon.\n"
                "- The **daemon's extension endpoint** (this plugin's "
                "concern) receives those events and forwards them to Fulcra "
                "as 'Attention' duration annotations.\n\n"
                "The bearer token below is a shared secret between the "
                "extension and the daemon — it stops random local pages from "
                "posting bogus data. It's separate from your Fulcra account."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Install the Fulcra Attention browser extension",
            body_md=(
                "**Fulcra Attention is in private beta** — there is no Chrome "
                "Web Store listing yet, so installation is currently from "
                "source. The steps below require the `fulcra-tools` repository "
                "to be checked out on your machine.\n\n"
                "**Chromium browsers only** (Chrome / Edge / Brave / Arc / "
                "Vivaldi). Firefox + Safari aren't supported yet.\n\n"
                "---\n\n"
                "**If you already have the fulcra-tools source checked out**, "
                "build the extension from that directory:\n\n"
                "```bash\n"
                "cd ~/Developer/fulcra-tools/packages/attention/chrome\n"
                "npm install\n"
                "npm run build\n"
                "```\n\n"
                "**If you don't have the source yet**, clone it first:\n\n"
                "```bash\n"
                "git clone https://github.com/ashfulcra/fulcra-tools.git "
                "~/fulcra-tools\n"
                "cd ~/fulcra-tools/packages/attention/chrome\n"
                "npm install\n"
                "npm run build\n"
                "```\n\n"
                "The built extension lands in "
                "`packages/attention/chrome/dist/` — that is the folder you "
                "will load in the next step.\n\n"
                "---\n\n"
                "**Load the built extension in your browser:**\n\n"
                "- Open `chrome://extensions/` (or `edge://extensions/`, "
                "`brave://extensions/` — copy/paste the URL; browsers block "
                "direct navigation to these pages).\n"
                "- Toggle **Developer mode** ON (top-right corner).\n"
                "- Click **Load unpacked**.\n"
                "- Select the **`packages/attention/chrome/dist/`** folder "
                "(the *built* output, not the `chrome/` source folder — "
                "loading `chrome/` directly will fail with "
                "\"Manifest file is missing or unreadable\").\n\n"
                "**Pin it.** Click the puzzle-piece toolbar icon and pin "
                "**Fulcra Attention** so its status badge is always "
                "visible.\n\n"
                "---\n\n"
                "*Once Fulcra Attention is on the Chrome Web Store, this step "
                "becomes a single click. See release notes for updates.*"
            ),
        ),
        SetupStep(
            kind="extension_pair",
            title="Pair the extension",
            body_md=(
                "Click the button below and the wizard will generate a fresh "
                "shared secret, hand it to the installed Fulcra Attention "
                "extension, and verify the handshake — no copy-paste, no "
                "options page.\n\n"
                "If the extension doesn't respond within 3 seconds (not "
                "installed yet, paused, or running in a different profile), "
                "a manual paste-token fallback will appear so you can finish "
                "setup the old way."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your attention data?",
            body_md=(
                "We can write to your existing 'Attention' annotation or "
                "create a new one. Attention events are duration "
                "annotations — one row per active-tab session."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="Attention is set",
            body_md=(
                "The daemon is now ready to receive extension events. "
                "Browse around for a minute, then check the dashboard's "
                "Recent activity — you should see Attention events landing."
            ),
        ),
    ),
)
