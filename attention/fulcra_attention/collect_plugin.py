"""fulcra-collect plugin: an informational pointer to the browser extension.

The Fulcra Attention browser extension is now fully relayless. It signs in
via its own browser-based device-flow OIDC and ingests events straight into
the Fulcra API — it no longer posts to the Fulcra Collect daemon, and there
is no longer a daemon-side relay route, pairing handshake, or shared
extension-token.

So this plugin no longer does any work. It exists purely so Collect still
surfaces an "Attention" entry in its plugin list: a signpost telling the
user to install the Fulcra Attention browser extension and sign in via the
browser. It declares no credentials, no setup steps, and no definition
binding; its run() emits a single informational message.

The rest of the `fulcra_attention` package (CLI / state / ingest /
definition_spec) is intentionally left in place — retiring it is a later
phase.
"""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext

# Where the browser extension lives in this repo, and the built unpacked
# output the user loads into their browser. Single-sourced here so the
# run() message and the description can't drift.
_EXTENSION_SOURCE_DIR = "attention/chrome"
_EXTENSION_BUILD_DIR = "attention/chrome/dist"

_POINTER_MESSAGE = (
    "Attention is captured by the Fulcra Attention browser extension, which "
    "signs in through your browser and sends data directly to Fulcra — there "
    "is nothing to configure here in Fulcra Collect.\n\n"
    "To start collecting attention data:\n"
    f"1. Build the extension from {_EXTENSION_SOURCE_DIR} (the built, "
    f"unpacked extension lands in {_EXTENSION_BUILD_DIR}).\n"
    f"2. Load {_EXTENSION_BUILD_DIR} as an unpacked extension in a Chromium "
    "browser (chrome://extensions → Developer mode → Load unpacked).\n"
    "3. Open the extension and sign in via your browser.\n\n"
    "The extension handles authentication and ingest on its own; the Fulcra "
    "Collect daemon is not involved."
)


def run(ctx: RunContext) -> None:
    """Emit a single informational message pointing at the browser extension.

    This plugin does no collection. The browser extension is relayless: it
    authenticates and ingests directly against the Fulcra API. We surface a
    one-line receipt in the dashboard so a user who clicks "Run now" gets a
    clear answer rather than silence.
    """
    ctx.log.info("attention pointer: directing user to the browser extension")
    ctx.progress(
        check="browser_extension",
        ok=True,
        detail=_POINTER_MESSAGE,
    )


PLUGIN = Plugin(
    id="attention-relay",
    name="Attention (browser extension)",
    # Manual + historical: there is no live data flow through the daemon
    # anymore. The extension is relayless, so from Collect's perspective
    # this entry is a static signpost, not a live collector. run() is a
    # one-shot informational message, fired only when the user clicks it.
    kind="manual",
    collect_mode="historical",
    run=run,
    description=(
        "Attention is collected by the Fulcra Attention browser extension, "
        "which signs in through your browser and sends data directly to "
        "Fulcra. Install the extension (built from "
        f"{_EXTENSION_SOURCE_DIR}, loaded unpacked from {_EXTENSION_BUILD_DIR}) "
        "and sign in via the browser. Nothing to configure in Fulcra Collect."
    ),
    category="activity",
)
