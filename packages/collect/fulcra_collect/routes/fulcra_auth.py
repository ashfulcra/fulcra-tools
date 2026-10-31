"""Collect-owned sign-in and refresh; no terminal credential adoption.

The historical split-route names remain for the served UI, but protocol 2 binds
completion to a durable attempt UUID and one captured device-code hash. Old
unscoped completion/classic requests refuse before credential effects.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException

from .. import config, credentials
from ..auth_attempts import AuthAttemptError
from ..owned_auth import CollectAuth, attempts
from ._deps import AuthCancelBody, CliDeviceCodeBody, FulcraTokenBody, RouteContext


def register(app: FastAPI, ctx: RouteContext) -> None:
    require_token = ctx.require_token

    def call(operation):
        try:
            return operation()
        except AuthAttemptError as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(503, 'Sign-in state is unavailable. Try again.') from None

    @app.get('/api/fulcra/auth/status', dependencies=[Depends(require_token)])
    def status():
        from ..config_leases import account_refresh_scope
        observation = {"availability": "unavailable", "saved_presence": None,
                       "checked_at": None, "provenance": "not_checked"}
        try:
            with account_refresh_scope(timeout_s=.2):
                if config.load().account_transition:
                    observation["availability"] = "transition"
                else:
                    observation = credentials.user_secret_availability('bearer-token')
        except Exception as exc:
            observation["availability"] = credentials.read_availability_error(exc)
        available = observation['availability']
        result = {**observation,
                  # Compatibility field: local readability only, never server validation.
                  'authenticated': True if available == 'present' else False if available == 'missing' else None,
                  'server_authenticated': None, 'refresh_available': None,
                  'refresh_failed': credentials.is_refresh_failed() if available == 'present' else False}
        if available not in ('present', 'missing'):
            result.update(code='keychain_access_required' if available == 'access_required' else 'keychain_' + available,
                          error='Saved sign-in access is unverified. Your credentials were kept.')
        return result

    @app.post('/api/fulcra/auth/keychain_access', dependencies=[Depends(require_token)])
    def authorize_keychain():
        from ..config_leases import account_refresh_scope
        items = {}
        try:
            with account_refresh_scope(timeout_s=.2):
                if config.load().account_transition:
                    raise credentials.CredentialStateChanged('Account transition in progress.')
                # One generation fences the WHOLE response, including a later
                # optional read or a same-account write between the two items.
                revision = credentials.credential_revision()
                # Explicit closed scope; optional absent refresh does not invalidate a manual bearer.
                for key in ('bearer-token', 'oauth-credentials'):
                    if credentials.credential_revision() != revision:
                        raise credentials.CredentialStateChanged('Credential state changed during authorization.')
                    try:
                        present = credentials.authorize_user_secret(key)
                        items[key] = {'status': 'present' if present else 'missing',
                                      'availability': 'present' if present else 'missing'}
                    except Exception as exc:
                        items[key] = {'status': 'unverified',
                                      'availability': credentials.read_availability_error(exc)}
                        if items[key]['availability'] in ('pending', 'changed'):
                            break  # No additional prompt while an earlier request is pending.
                if credentials.credential_revision() != revision:
                    raise credentials.CredentialStateChanged('Credential state changed during authorization.')
        except Exception as exc:
            # Previously observed items no longer establish current response
            # authority after a whole-operation refusal. Unvisited items remain
            # not_checked; never reuse an earlier bearer success.
            availability = credentials.read_availability_error(exc)
            for key in set(items) | {'bearer-token'}:
                items[key] = {'status': 'unverified', 'availability': availability}
        for key in ('bearer-token', 'oauth-credentials'):
            items.setdefault(key, {'status': 'unverified', 'availability': 'not_checked'})
        ok = items['bearer-token']['status'] == 'present'
        result = {'ok': ok, 'items': items, 'refresh_item_readable': items['oauth-credentials']['status'] == 'present'}
        if not ok:
            result.update(code='keychain_access_required',
                          error='Saved sign-in access is unverified or missing. Finish any pending request, then check again.')
        return result

    @app.post('/api/fulcra/auth/token', dependencies=[Depends(require_token)])
    def set_token(body: FulcraTokenBody):
        token = body.token.strip()
        if not token:
            raise HTTPException(400, 'token is empty')
        return call(lambda: CollectAuth().set_token(token))

    @app.delete('/api/fulcra/auth/token', dependencies=[Depends(require_token)])
    def clear():
        return call(lambda: CollectAuth().clear())

    @app.get('/api/fulcra/auth/cli_status', dependencies=[Depends(require_token)])
    def available():
        # Compatibility name only: availability no longer depends on PATH,
        # executing a CLI, or the CLI's unrelated signed-in account.
        return {'available': True, 'signed_in': False, 'protocol': 2}

    @app.post('/api/fulcra/auth/cli_login', dependencies=[Depends(require_token)])
    def retired():
        raise HTTPException(409, 'Refresh Collect and sign in using the browser link.')

    @app.post('/api/fulcra/auth/cli_login_start', dependencies=[Depends(require_token)])
    def start():
        return call(lambda: CollectAuth().start())

    @app.post('/api/fulcra/auth/cli_login_poll', dependencies=[Depends(require_token)])
    def poll(body: CliDeviceCodeBody):
        if body.protocol != 2 or not body.attempt_id:
            raise HTTPException(409, 'Refresh Collect and start a new browser sign-in.')
        return call(lambda: CollectAuth().poll(body.attempt_id, body.device_code))

    @app.post('/api/fulcra/auth/cancel', dependencies=[Depends(require_token)])
    def cancel(body: AuthCancelBody):
        def operation():
            attempts().cancel(body.attempt_id)
            return {'ok': True}
        return call(operation)
