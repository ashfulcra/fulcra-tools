"""fulcra-collect plugin: the local Gmail relay daemon.

A ``scheduled`` collect plugin (default every 15 min). Each poll walks
``authorized_accounts × applicable_rules`` and, per pair, runs the B1
contiguous-frontier sync (:func:`fulcra_gmail.pipeline.poll_account_rule`):
fully paginate the rule's server ``q``, refine candidates to effective matches
locally, land each match's selected-email JSON in Fulcra Files, and relay it on
the operator's coord bus — all crash-safe and idempotent, all account-scoped by
opaque ``account_id``. One account's auth failure is fail-soft: it is skipped
with a health warning while the others proceed.

**Setup wizard.** ``setup_steps`` walk the operator through creating ONE
Google Workspace *Internal Web* OAuth client (exact Cloud-Console click-path
below), pasting the shared client id/secret, then a **repeatable add-account**
leg that mints a single-use nonce, opens Google consent, and binds the granted
token to the account discovered via ``users.getProfile`` (B4). Adding a second
account is the same leg again.

**Credentials.** The shared OAuth client lives in the keychain under the plugin
namespace (keys ``client:client_id`` / ``client:client_secret``, which is exactly
what :class:`fulcra_gmail.accounts.AccountRegistry` reads); per-account refresh
tokens live at ``account:<account_id>:refresh_token``. Rules + the coord relay
team are non-secret config.

Credit: the original Gmail-relay design is ArcBot's (openclaw); this is a
clean-room rebuild on current main.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fulcra_collect.plugin import (
    Credential,
    HealthResult,
    Plugin,
    RunContext,
    Setting,
    SetupStep,
)

from .accounts import STATUS_AUTH_FAILED, AccountRegistry
from .client import GmailClient
from .cursors import CursorStore
from .files_writer import build_files_writer
from .ledger import Ledger
from .pipeline import poll_account_rule
from .relay import CoordEngineRelayEmitter
from .rules import parse_rules

_log = logging.getLogger("fulcra_gmail.collect_plugin")

PLUGIN_ID = "gmail"
#: The OAuth callback collect already serves (Trakt-proven).
REDIRECT_URI = "http://127.0.0.1:9292/api/oauth/callback"


# ---------------------------------------------------------------------------
# Add-account bridge (driven by the wizard / host callback route)
# ---------------------------------------------------------------------------


def _registry(transport=None) -> AccountRegistry:
    """Build the production registry (keychain + JSON store under collect home)."""
    return AccountRegistry(transport=transport)


def begin_add_account(redirect_uri: str = REDIRECT_URI):
    """Mint a nonce + PKCE and return the add-account session (incl. authorize
    URL). The nonce is the OAuth ``state``; the account is bound later from
    ``getProfile`` (B4), never from any operator-typed hint."""
    return _registry().begin_add_account(redirect_uri)


def complete_add_account(state: str | None, code: str):
    """Finish an add-account flow: consume the nonce once, exchange the code,
    discover the address via ``getProfile``, and write the registry row +
    keychain token. See :meth:`AccountRegistry.complete_add_account`."""
    return _registry().complete_add_account(state, code)


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------


def _load_rules(ctx: RunContext):
    raw = ctx.config.get("rules") or []
    if not raw:
        return []
    return parse_rules(list(raw))


def run(ctx: RunContext) -> None:
    """One poll pass across every authorized account × applicable rule."""
    rules = _load_rules(ctx)
    if not rules:
        ctx.log.info("gmail: no rules configured — nothing to poll")
        return
    relay_team = ctx.config.get("relay_team")
    if not relay_team:
        ctx.log.warning("gmail: no relay_team configured — relay actions cannot emit")

    registry = _registry()
    accounts = registry.list_accounts()
    if not accounts:
        ctx.log.info("gmail: no authorized accounts — run the add-account wizard")
        return

    files_writer = build_files_writer(ctx.fulcra_token())
    relay_emitter = (
        CoordEngineRelayEmitter(relay_team) if relay_team else None
    )

    for account in accounts:
        if account.status == STATUS_AUTH_FAILED:
            ctx.log.warning("gmail: account %s is auth_failed — skipping (re-auth needed)",
                            account.account_id)
            ctx.progress(account=account.account_id, status="auth_failed")
            continue
        client = GmailClient(account.account_id, registry=registry)
        ledger = Ledger(account.account_id)
        cursors = CursorStore(account.account_id)
        for rule in rules:
            if not rule.applies_to_account(account.account_id, account.email):
                continue
            try:
                result = poll_account_rule(
                    client=client, rule=rule, account_id=account.account_id,
                    ledger=ledger, cursors=cursors, files_writer=files_writer,
                    relay_emitter=relay_emitter,
                )
            except Exception as exc:  # noqa: BLE001 — one rule's failure is soft
                ctx.log.warning("gmail: poll failed account=%s rule=%s: %s",
                                account.account_id, rule.id, type(exc).__name__)
                continue
            ctx.progress(
                account=account.account_id, rule=rule.id,
                candidates=result.candidates, effective=result.effective,
                processed=result.processed, blocked=result.blocked,
            )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _cursor_age_hours(cursor_epoch: int | None, *, now: datetime) -> float | None:
    if cursor_epoch is None:
        return None
    return (now - datetime.fromtimestamp(cursor_epoch, tz=timezone.utc)).total_seconds() / 3600


def health_check(ctx: RunContext) -> HealthResult:
    """Per-account health: auth ok / auth-failed surfaced, oldest cursor age,
    ledger tail. Progress is keyed by ``account_id``."""
    try:
        registry = _registry()
        accounts = registry.list_accounts()
    except Exception as exc:  # noqa: BLE001
        return HealthResult(ok=False, summary=f"registry unavailable: {type(exc).__name__}")
    if not accounts:
        return HealthResult(ok=False, summary="No Gmail accounts authorized yet.")

    rules = _load_rules(ctx)
    now = datetime.now(timezone.utc)
    preview: list[dict] = []
    failed = 0
    for account in accounts:
        auth_ok = account.status != STATUS_AUTH_FAILED
        if not auth_ok:
            failed += 1
        cursors = CursorStore(account.account_id)
        ledger = Ledger(account.account_id)
        oldest_age = None
        for rule in rules:
            if not rule.applies_to_account(account.account_id, account.email):
                continue
            age = _cursor_age_hours(cursors.get(rule.id, rule.version), now=now)
            if age is not None and (oldest_age is None or age > oldest_age):
                oldest_age = age
        entries = ledger.entries()
        tail_ts = entries[-1].get("ts") if entries else None
        preview.append({
            "account_id": account.account_id,
            "auth": "ok" if auth_ok else "auth_failed",
            "oldest_cursor_age_h": round(oldest_age, 1) if oldest_age is not None else None,
            "ledger_entries": len(entries),
            "ledger_tail": tail_ts,
        })
    ok = failed == 0
    if failed:
        summary = f"{failed}/{len(accounts)} account(s) need re-authorization."
    else:
        summary = f"{len(accounts)} Gmail account(s) authorized."
    return HealthResult(ok=ok, summary=summary, preview=preview)


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


_CLOUD_CONSOLE_CLICKPATH = (
    "Create the OAuth client **once** in the Google Cloud Console for your "
    "Workspace:\n\n"
    "1. Go to **console.cloud.google.com** → pick (or create) a project.\n"
    "2. **APIs & Services → Enabled APIs & services → + Enable APIs** → enable "
    "the **Gmail API**.\n"
    "3. **APIs & Services → OAuth consent screen** → **User Type: Internal** "
    "→ Create. (Internal needs no Google verification for the restricted "
    "`gmail.readonly` scope and has no 7-day token expiry.)\n"
    "4. **APIs & Services → Credentials → + Create Credentials → OAuth client "
    "ID** → **Application type: Web application**.\n"
    "5. Under **Authorized redirect URIs** add EXACTLY: "
    f"`{REDIRECT_URI}` (one entry, no trailing slash).\n"
    "6. Create → copy the **Client ID** and **Client secret**.\n\n"
    "Scope requested: `https://www.googleapis.com/auth/gmail.readonly` ONLY — "
    "the relay never modifies, sends, or deletes mail."
)


PLUGIN = Plugin(
    id=PLUGIN_ID,
    name="Gmail relay",
    kind="scheduled",
    collect_mode="live_polled",
    run=run,
    description=(
        "Polls your authorized Gmail account(s) read-only with local filter "
        "rules, lands selected emails in your Fulcra Files, and relays matches "
        "to an agent on your coord bus. Multi-account; nothing leaves the "
        "machine except the artifacts your rules select."
    ),
    default_interval=timedelta(minutes=15),
    requires_network=True,
    category="other",
    required_credentials=(
        Credential(
            key="client:client_id",
            label="Google OAuth client ID",
            help="The shared Workspace OAuth client ID (one app for all accounts).",
        ),
        Credential(
            key="client:client_secret",
            label="Google OAuth client secret",
            help="The shared Workspace OAuth client secret.",
        ),
    ),
    required_settings=(
        Setting(
            key="relay_team",
            label="Coord relay team/space",
            kind="text",
            required=False,
            help=(
                "The coord-engine team a matched email's relay directive is "
                "sent to (the receipt-capture agent's space). Leave blank to "
                "file only, without relaying."
            ),
        ),
        Setting(
            key="rules",
            label="Filter rules",
            kind="long_text",
            required=False,
            help=(
                "Relay rules live in config.toml as [[plugin_settings.gmail."
                "rules]] tables: id, version, name, match (Gmail q), optional "
                "from_regex/subject_regex/has_attachment, actions "
                "([\"file\",\"relay\"]), relay_to, relay_priority, optional "
                "accounts (ids/emails; omit = all)."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="How the Gmail relay works",
            body_md=(
                "The relay polls your Gmail account(s) read-only every 15 "
                "minutes, applies your local filter rules entirely on this "
                "machine, and only ever exports the emails your rules select — "
                "into your own Fulcra Files, plus a directive on your coord "
                "bus. Locally-rejected mail leaves zero trace."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Create the Google Workspace OAuth client",
            body_md=_CLOUD_CONSOLE_CLICKPATH,
            external_link="https://console.cloud.google.com/apis/credentials",
        ),
        SetupStep(
            kind="input",
            title="Paste your OAuth client id + secret",
            body_md=(
                "Paste the Client ID and Client secret from the step above. "
                "They are stored in your OS keychain, never in config or logs, "
                "and shared across every account you add."
            ),
            settings_keys=("client:client_id", "client:client_secret"),
        ),
        SetupStep(
            kind="external_action",
            title="Add a Gmail account (repeatable)",
            body_md=(
                "Click below to authorize a Gmail account. You'll be sent to "
                "Google's consent screen — pick whichever Google account you "
                "want to relay; the plugin binds to whatever you actually "
                "authorize (discovered from the token via getProfile). Run "
                "this step again for each additional account."
            ),
        ),
        SetupStep(
            kind="input",
            title="Point at your filter rules",
            body_md=(
                "Add one or more [[plugin_settings.gmail.rules]] tables to "
                "config.toml (see the Filter rules setting for the schema), "
                "then set the coord relay team below."
            ),
            settings_keys=("relay_team", "rules"),
        ),
        SetupStep(
            kind="test_connection",
            title="Test the connection",
            body_md=(
                "We'll run one query probe per authorized account to confirm "
                "the tokens work and mail is reachable."
            ),
        ),
        SetupStep(
            kind="done",
            title="Gmail relay is set",
            body_md=(
                "The relay polls every 15 minutes. Matches land under "
                "/collect/gmail/<account>/<yyyy-mm>/ in your Files and relay to "
                "your coord team. Add more accounts anytime by re-running the "
                "add-account step."
            ),
        ),
    ),
    health_check=health_check,
)
