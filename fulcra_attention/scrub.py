"""Tier 1 URL scrubbing — pure function.

Strip auth-bearing query params, tracking params, one-click action tokens.
Whole fragment dropped by default (covers OAuth Implicit Flow + Slack/Notion
magic-share links).

Cross-language contract: a sibling TypeScript implementation in the Chrome
extension (Plan B) must produce identical output for identical input.
Shared fixture file lives in `tests/fixtures/scrub_cases.json` (Plan B).
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Lowercase for case-insensitive matching.
DENYLIST: frozenset[str] = frozenset({
    # auth-bearing — OAuth 2 + plain
    "access_token", "id_token", "refresh_token", "code", "state", "nonce",
    "client_secret", "assertion", "session", "sid", "sessionid", "auth",
    "authorization", "token", "apikey", "api_key", "key", "signature",
    "sig", "hmac", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
    "magic", "share_token", "invite", "confirmation_token",
    "_csrf", "csrf_token", "xsrf", "ticket", "ott",
    # OAuth 1.0a (Twitter/Trello/many self-hosted apps) — these are
    # literal session credentials in the query string.
    "oauth_token", "oauth_verifier", "oauth_signature", "oauth_callback",
    "oauth_consumer_key", "oauth_nonce", "oauth_timestamp",
    # tracking
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
    "igshid", "yclid", "ref", "ref_src", "ref_url",
    # one-click action tokens
    "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
    # magic-link identifiers (single-use login over email)
    "email", "username", "login", "magic_link",
})


def scrub_url(url: str) -> str:
    """Return `url` with denylisted query params and the entire fragment dropped.

    Also normalizes to canonical browser-URL form (matches `new URL()` in JS):
      - Empty path → "/"
      - Strip default ports (:443 for https, :80 for http)
      - Strip userinfo from authority

    Pure function. Preserves param order of surviving entries.
    """
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for (k, v) in pairs if k.lower() not in DENYLIST]
    new_query = urlencode(kept)

    # Canonicalize the authority to match browser URL behavior.
    netloc = parts.hostname or ""
    port = parts.port
    if port is not None:
        is_default = (
            (parts.scheme == "https" and port == 443)
            or (parts.scheme == "http" and port == 80)
        )
        if not is_default:
            netloc = f"{netloc}:{port}"
    # Userinfo (parts.username / parts.password) is intentionally dropped.

    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, new_query, ""))
