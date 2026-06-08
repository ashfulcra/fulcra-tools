# AGENTS.md — autoloaded by Aider / Cursor / Continue.dev / Claude Code / OpenHands

fulcra-attention is **fully relayless**. The real work lives in the Chrome MV3 extension under `chrome/`: it signs in with an Auth0 device flow and POSTs records **directly to the Fulcra API** (`https://api.fulcradynamics.com/ingest/v1/record/batch`). There is no localhost daemon involvement, no pairing, no per-extension token, and no relay route — the relay-era Python backend (CLI / `ingest.py` / `fulcra.py` / `state.py`) has been retired.

The Python package (`fulcra_attention/`) is now just the Fulcra Collect *pointer* plugin (`collect_plugin.py`): it does no collection and only surfaces an "Attention" entry that tells the user to install the browser extension and sign in via the browser.

Key extension details: per-browser identity is a `machine:<slug>` tag derived from the browser's identity label; records use the `com.fulcra.attention.v3.` source_id namespace (folding the identity slug into the hash for multi-browser distinctness). Cross-check `chrome/src/relayless/*` (`oidc.ts`, `signIn.ts`, `relaylessSender.ts`, `ensureDefinition.ts`, `wire.ts`, `config.ts`) and `fulcra_attention/collect_plugin.py` before making claims about the flow.
