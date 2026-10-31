"""Collect-owned SDK device grants; no terminal credential-store adoption."""
from __future__ import annotations

import hashlib
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import httpx

from . import config, credentials
from .auth_attempts import AuthAttempts, AuthAttemptError


def provider():
    from fulcra_api.core import (
        FULCRA_OIDC_DOMAIN, FULCRA_OIDC_CLIENT_ID, FULCRA_OIDC_SCOPE, FULCRA_OIDC_AUDIENCE,
    )
    from fulcra_api.oidc import FulcraOIDCProvider
    if FULCRA_OIDC_DOMAIN != 'fulcra.us.auth0.com':
        raise AuthAttemptError('This Collect build has an unsupported sign-in provider.')
    return FulcraOIDCProvider(FULCRA_OIDC_DOMAIN, FULCRA_OIDC_CLIENT_ID,
                              FULCRA_OIDC_SCOPE, FULCRA_OIDC_AUDIENCE)


def attempts():
    return AuthAttempts(config.config_dir() / 'auth-attempts.sqlite3')


def validate_token(token):
    """The JWT supplies a candidate UUID; the API verifies token and identity."""
    from fulcra_common import DEFAULT_BASE_URL
    principal = credentials._token_principal(token, fresh=True)
    if principal is None:
        raise AuthAttemptError('The sign-in token is invalid or expired. Start again.')
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        with client.stream('GET', f'{DEFAULT_BASE_URL}/user/v1alpha1/info',
                           headers={'Authorization': f'Bearer {token}', 'Accept-Encoding': 'identity'}) as response:
            response.raise_for_status()
            # Bound wire bytes before decoding. iter_bytes would decompress an
            # encoded body before our cap could reject it.
            if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                raise AuthAttemptError('Could not verify the signed-in account. Start again.')
            chunks, size = [], 0
            for chunk in response.iter_raw(chunk_size=16384):
                size += len(chunk)
                if size > 65536:
                    raise AuthAttemptError('Could not verify the signed-in account. Start again.')
                chunks.append(chunk)
    value = json.loads(b''.join(chunks))
    if not isinstance(value, dict) or value.get('userid') != principal:
        raise AuthAttemptError('Could not verify the signed-in account. Start again.')


def _failure():
    return AuthAttemptError('Sign-in could not be verified or has changed. Start again.')


class CollectAuth:
    def __init__(self, *, journal=None, provider=None, validate=None):
        self.journal = journal if journal is not None else attempts()
        self.provider = provider  # Lazy: manual paste/signout need no SDK provider.
        self.validate = validate if validate is not None else validate_token

    def _provider(self):
        if self.provider is None:
            self.provider = provider()
        return self.provider

    def start(self):
        attempt = self.journal.begin('device')
        try:
            code, uri, web_code, expiry, interval = self._provider().get_device_code()
            if (not isinstance(uri, str) or len(uri) > 4096 or not isinstance(web_code, str)
                    or not 0 < len(web_code) <= 64):
                raise _failure()
            url = urlsplit(uri)
            if (url.scheme != 'https' or url.netloc != 'fulcra.us.auth0.com'
                    or parse_qs(url.query).get('user_code') != [web_code]):
                raise _failure()
            self.journal.bind(attempt, code, expiry, interval)
            return {'protocol': 2, 'attempt_id': attempt, 'auth_url': uri,
                    'web_auth_code': web_code, 'device_code': code,
                    'expires_in': expiry, 'interval': max(5, interval)}
        except Exception:
            self.journal.fail(attempt)
            raise _failure() from None

    def poll(self, attempt, device_code):
        wait = self.journal.claim(attempt, device_code)
        if wait is not None:
            return self._pending(wait)
        try:
            result = self._provider().get_token('urn:ietf:params:oauth:grant-type:device_code',
                                                {'device_code': device_code})
        except HTTPError as exc:
            try:
                body = exc.read(4097)
                error = json.loads(body) if len(body) <= 4096 else {}
                if not isinstance(error, dict):
                    error = {}
            except (ValueError, OSError):
                error = {}
            finally:
                exc.close()
            if exc.code == 429 or (exc.code == 403 and error.get('error') in ('authorization_pending', 'slow_down')):
                return self._pending(self.journal.pending(
                    attempt, slow=exc.code == 429 or error.get('error') == 'slow_down'))
            self.journal.fail(attempt)
            raise _failure() from None
        except Exception:
            self.journal.fail(attempt)
            raise _failure() from None
        try:
            token, refresh = result.access_token, result.refresh_token
            if (not isinstance(token, str) or not 0 < len(token) <= 65536
                    or credentials._token_principal(token, fresh=True) is None
                    or not isinstance(refresh, str) or not 0 < len(refresh) <= 16384):
                raise _failure()
            self.validate(token)
            def commit():
                if credentials._token_principal(token, fresh=True) is None:
                    raise _failure()
                credentials.set_user_secret('oauth-credentials', json.dumps({
                    'version': 1, 'attempt_id': attempt, 'access_token': token,
                    'refresh_token': refresh}, sort_keys=True))
                credentials.set_user_secret('bearer-token', token)
                credentials.clear_refresh_failed()
            self.journal.finish(attempt, commit, transition=config.fulcra_account_transition)
            return {'ok': True, 'protocol': 2}
        except Exception:
            self.journal.fail(attempt)
            raise _failure() from None

    @staticmethod
    def _pending(wait):
        return {'ok': False, 'pending': True, 'protocol': 2, 'retry_after': wait}

    def set_token(self, token):
        attempt = self.journal.begin('token')
        try:
            if (not isinstance(token, str) or not 0 < len(token) <= 65536
                    or credentials._token_principal(token, fresh=True) is None):
                raise _failure()
            self.validate(token)
            def commit():
                if credentials._token_principal(token, fresh=True) is None:
                    raise _failure()
                credentials.delete_user_secret('oauth-credentials')
                credentials.set_user_secret('bearer-token', token)
                credentials.clear_refresh_failed()
            self.journal.finish(attempt, commit, transition=config.fulcra_account_transition)
        except Exception:
            self.journal.fail(attempt)
            raise _failure() from None
        return {'ok': True}

    def clear(self):
        attempt = self.journal.begin('clear')
        try:
            def commit():
                credentials.delete_user_secret('oauth-credentials')
                credentials.delete_user_secret('bearer-token')
                credentials.clear_refresh_failed()
            self.journal.finish(attempt, commit, transition=config.fulcra_account_transition)
        except Exception:
            self.journal.fail(attempt)
            raise _failure() from None
        return {'ok': True}


def refresh_owned(*, expected_token=None):
    """Return (handled, token). Only pre-upgrade authority uses legacy renewal.

    New interactive device grants have their own rotating refresh credential.
    Manual tokens require a fresh explicit sign-in when expired. A cross-process
    lease serializes grant consumption; the final auth/account fences prevent a
    late refresh from undoing any newer interactive credential change.
    """
    from .config_leases import account_refresh_scope
    journal = attempts()
    try:
        with journal.refresh_scope():
            with account_refresh_scope():
                revision = journal.read_committed()
                if revision == (None, None):
                    return False, None
                if revision[1] != 'device':
                    return True, None
                original = credentials.get_user_secret_fresh('bearer-token')
                principal = credentials._token_principal(original)
                raw = credentials.get_user_secret_fresh('oauth-credentials')
                if config.load().account_transition or principal is None or not raw or len(raw) > 131072:
                    return True, None
                if expected_token is not None:
                    if credentials._token_principal(expected_token) != principal:
                        return True, None
                    if original != expected_token and credentials._token_principal(original, fresh=True) == principal:
                        return True, original
                stored = json.loads(raw)
                if (not isinstance(stored, dict) or set(stored) != {'version', 'attempt_id', 'access_token', 'refresh_token'}
                        or type(stored['version']) is not int or stored['version'] != 1
                        or stored['attempt_id'] != revision[0] or stored['access_token'] != original
                        or not isinstance(stored['refresh_token'], str)
                        or not 0 < len(stored['refresh_token']) <= 16384):
                    return True, None
                fingerprint = hashlib.sha256(raw.encode()).hexdigest()
                journal.claim_refresh(revision, fingerprint)
            oidc = provider()
            result = oidc.get_token('refresh_token', {'refresh_token': stored['refresh_token'], 'scope': oidc.scope})
            token = result.access_token
            refresh = result.refresh_token if result.refresh_token is not None else stored['refresh_token']
            if (credentials._token_principal(token, fresh=True) != principal
                    or not isinstance(refresh, str) or not 0 < len(refresh) <= 16384):
                return True, None
            validate_token(token)
            with account_refresh_scope():
                with journal.refresh_commit_scope(revision, fingerprint):
                    if (config.load().account_transition
                            or credentials.get_user_secret_fresh('bearer-token') != original
                            or credentials.get_user_secret_fresh('oauth-credentials') != raw):
                        return True, None
                    # New refresh first: if the access-token write fails, mismatch
                    # is explicit and requires sign-in rather than grant replay.
                    credentials.set_user_secret_quiet('oauth-credentials', json.dumps({
                        'version': 1, 'attempt_id': revision[0], 'access_token': token,
                        'refresh_token': refresh}, sort_keys=True))
                    credentials.set_user_secret_quiet('bearer-token', token)
            return True, token
    except Exception:
        # No SDK exception/body/token in logs or API errors; never fall back to
        # the CLI after an owned-store, grant, or authority failure.
        return True, None
