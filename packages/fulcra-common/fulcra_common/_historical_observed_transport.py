"""Inactive prepared transport mechanics. No production construction is available.

Tests alone construct these private objects with object.__new__. A response from
this module is an observation, never admission, ownership or replay authority.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field

import httpx

from .record_observations import _HEADERS, _canonical, _hooks, _now, _url_origin


def _ref(ref):
    return {'artifact_id': str(ref.artifact_id), 'sha256': ref.sha256}


def _blob(body):
    return {'sha256': hashlib.sha256(body).hexdigest(), 'byte_length': len(body),
            'bytes_base64': base64.b64encode(body).decode('ascii')}


@dataclass(frozen=True, repr=False)
class _ReceivedFragment:
    canonical: bytes


@dataclass(frozen=True, repr=False)
class _PreparedRequest:
    owner: object = field(repr=False)
    _request: httpx.Request = field(repr=False)
    seal: tuple = field(repr=False)


def _seal(request):
    return (request.method, str(request.url), tuple(request.headers.raw), request.content,
            tuple(sorted(request.extensions.get('timeout', {}).items())))


class _ClosedObservedClient:
    def __init__(self):
        raise ValueError('historical client construction is inactive: H1-H5 remain held')

    def get_token(self):
        # This view never calls BaseFulcraClient, Collect, OAuth, a store or CLI.
        self._http._check()
        return self._admission.bearer

    def _client(self):
        self._http._check()
        return self._http


class _ClosedObservedHTTP:
    def __init__(self):
        raise ValueError('historical transport construction is inactive: H3/H4 remain held')

    @property
    def base_url(self):
        return self._http.base_url

    def _check(self):
        http = self._http
        if (type(http) is not httpx.Client or http.auth is not None or http.cookies
                or http.follow_redirects or http.trust_env or http._mounts
                or any(http.event_hooks.values()) or http._transport is not self._transport
                or self._owner._http is not self or self._owner._admission is not self._admission
                or self._owner._gate is not self._gate or http.base_url.scheme != 'https'):
            raise ValueError('unsupported historical client state')
        if any(value is None or not 0 < value <= 30 for value in http.timeout.as_dict().values()):
            raise ValueError('unsupported historical timeout')

    def prepare(self, method, path, *, headers, params=None, content=b'', timeout=None):
        self._check()
        options = {} if timeout is None else {'timeout': timeout}
        request = self._http.build_request(method, path, headers=headers, params=params,
                                           content=content, **options)
        return _PreparedRequest(self, request, _seal(request))


def _closed(http):
    return type(http) is _ClosedObservedHTTP


def _send_prepared(http, *, method, path, headers, kind, context, request_guard,
                   request_budget, params=None, content=b'', timeout=None, prepared=None):
    if not _closed(http):
        raise ValueError('historical dispatch requires its exact owned adapter')
    if prepared is None:
        prepared = http.prepare(method, path, headers=headers, params=params, content=content, timeout=timeout)
    return _dispatch_observed(prepared=prepared, kind=kind, context=context,
        request_guard=request_guard, request_budget=request_budget,
        admission=http._admission, gate=http._gate)


def _check_prepared(prepared, kind, context, admission):
    request = prepared._request
    prepared.owner._check()
    timeouts = request.extensions.get('timeout', {})
    if (set(timeouts) != {'connect', 'read', 'write', 'pool'}
            or any(type(value) not in {int, float} or not 0 < value <= 30 for value in timeouts.values())):
        raise ValueError('unsupported historical prepared timeout')
    if _seal(request) != prepared.seal:
        raise ValueError('historical prepared request changed')
    origin = _url_origin(prepared.owner.base_url)
    authorization = request.headers.get_list('authorization')
    if (len(authorization) != 1 or not hmac.compare_digest(authorization[0], 'Bearer ' + admission.bearer)
            or _url_origin(request.url) != origin or admission.account_id != context.account_id):
        raise ValueError('historical prepared scope or authorization changed')
    expected = {'owned-list': ('GET', '/input/v1/data_type'),
                'schema-get': ('GET', f'/data/v1/catalog/{context.destination_data_type}/v1/schema'),
                'record-get': ('GET', '/data/v1/records'),
                'record-post': ('POST', f'/ingest/v1/record/{context.destination_data_type}')}
    if kind not in expected or (request.method, request.url.path) != expected[kind]:
        raise ValueError('historical request kind/scope changed')
    query = list(request.url.params.multi_items())
    if (kind == 'owned-list' and query != [('include_deprecated', 'true')]
            or kind == 'schema-get' and query
            or kind == 'record-post' and query != [('api_version', 'v1')]
            or kind == 'record-get' and (len(query) != 2 or query[0][0] != 'q'
                                         or query[1] != ('fulcra_userid', context.account_id))):
        raise ValueError('historical request query changed')


def _fragment(response, *, kind, context, admission_ref, started_at):
    request = response.request
    headers = [[name.decode('ascii').lower(), value.decode('latin-1')]
               for name, value in response.headers.raw if name.decode('ascii').lower() in _HEADERS]
    result = {'request': {'method': request.method, 'origin': _url_origin(request.url),
        'path': request.url.path, 'ordered_query': [list(pair) for pair in request.url.params.multi_items()],
        'destination_data_type': context.destination_data_type,
        'requested_target_ids': list(map(str, context.requested_target_ids))},
        'response': {'status': response.status_code, 'origin': _url_origin(response.url),
        'path': response.url.path, 'ordered_query': [list(pair) for pair in response.url.params.multi_items()],
        'redirect_count': len(response.history), 'body_byte_length': len(response.content),
        'body_sha256': hashlib.sha256(response.content).hexdigest(),
        'headers': {name: response.headers.get(name) for name in _HEADERS}},
        'raw_request_target': base64.b64encode(request.url.raw_path).decode('ascii'),
        'request_body': _blob(request.content), 'response_body': _blob(response.content),
        'response_header_occurrences': headers}
    return _ReceivedFragment(_canonical({'request_kind': kind, 'admission_ref': admission_ref,
        'started_at': started_at, 'completed_at': _now(), 'result': result}))


def _dispatch_observed(*, prepared, kind, context, request_guard, request_budget, admission, gate) -> httpx.Response:
    if type(prepared) is not _PreparedRequest or not _closed(prepared.owner):
        raise ValueError('historical prepared owner unavailable')
    http = prepared.owner
    if admission is not http._admission or gate is not http._gate:
        raise ValueError('historical admission/gate binding differs')
    _hooks(request_guard, request_budget, context)
    started_at = _now()
    with gate.hold_current(admission, context=context, mutating=kind == 'record-post'):
        _check_prepared(prepared, kind, context, admission)
        request_budget.charge_request(kind)
        request_guard(kind=kind, context=context)
        # No callbacks or authority reads between this seal assertion and send.
        _check_prepared(prepared, kind, context, admission)
        try:
            response = http._http.send(prepared._request, auth=None, follow_redirects=False)
        except Exception:
            # Library exceptions can embed request headers. Keep secrets out of errors.
            raise ValueError('historical transport failed; request outcome unknown') from None
    if http._capture is not None:
        fragment = _fragment(response, kind=kind, context=context,
            admission_ref=_ref(http._owner._admission_ref), started_at=started_at)
        http._capture.append_received(fragment)
    return response
