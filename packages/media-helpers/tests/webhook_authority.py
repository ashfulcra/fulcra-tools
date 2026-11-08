"""Synthetic configured media authority shared by webhook integration harnesses."""
import base64
import json
import os
import re
import threading

import httpx
import pytest
from datetime import datetime, timezone

from fulcra_common.client import BaseFulcraClient
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_media.media_config import CATEGORIES, FILENAME
from fulcra_media.v1 import media_spec

ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
PINS = {key: 'Event/' + str(index) * 8 + '-2222-4333-8444-555555555555'
        for index, key in enumerate(CATEGORIES, 1)}
CONTROL = 'Event/aaaaaaaa-2222-4333-8444-555555555555'


class Authority:
    def __init__(self, root):
        self.root = root
        root.mkdir(exist_ok=True)
        claims = base64.urlsafe_b64encode(json.dumps({
            'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800,
        }).encode()).decode().rstrip('=')
        os.environ['FULCRA_COLLECT_HOME'] = str(root)
        self.token = 'synthetic.' + claims + '.signature'
        os.environ['FULCRA_ACCESS_TOKEN'] = self.token
        self.settings = {'version': 1, 'account_id': ACCOUNT,
                         'v1_destination_ids': dict(PINS),
                         'legacy_selection': dict.fromkeys(CATEGORIES)}
        (root / FILENAME).write_text(json.dumps(self.settings))
        CreationJournal(root / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
        self.rows, self.posts, self.threads = {}, [], []
        self.account = ACCOUNT
        self.mode = 'visible'
        self.before_post = lambda: None
        self.client = BaseFulcraClient(base_url='https://example.test',
                                      transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.threads.append(threading.get_ident())
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': self.account})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{
                'id': full, 'fulcra_userid': ACCOUNT, 'name': spec.name,
                'api_version': 'v1', 'recordable': True, 'deprecated': False,
                'record_spec': {'type': 'event'},
            } for full, spec in [(PINS[c], media_spec(c)) for c in CATEGORIES]
              + [(CONTROL, EXCLUSION_SPEC)]])
        if path.endswith('/schema'):
            spec = next((media_spec(c) for c in CATEGORIES if PINS[c] in path), EXCLUSION_SPEC)
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **dict.fromkeys(('id', 'sources', 'tags', 'start_time', 'end_time'), {}),
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': '11111111-1111-4111-8111-111111111111',
                                            'name': request.url.path.rsplit('/', 1)[-1] if '/tag/name/' in path else 'synthetic', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            if self.mode == 'unreadable':
                return httpx.Response(503)
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            sources = re.findall('source="([^"]+)"', query)
            if sources:
                ids = [i for (kind, i), row in self.rows.items()
                       if kind == full and set(row['sources']).intersection(sources)]
            return httpx.Response(200, text='\n'.join(json.dumps(self.rows[full, i])
                                  for i in ids if (full, i) in self.rows))
        if path == '/ingest/v1/record/' + PINS['watched']:
            self.before_post()
            batch = [json.loads(line) for line in request.content.splitlines()]
            self.posts.extend(batch)
            if self.mode == 'rejected':
                return httpx.Response(400)
            if self.mode != 'unknown':
                self.rows.update({(PINS['watched'], row['id']): row for row in batch})
            if self.mode in ('lost', 'unknown'):
                raise httpx.ReadError('synthetic lost receipt')
            return httpx.Response(200, json={'upload_id': 'synthetic-upload'})
        raise AssertionError('unexpected synthetic path ' + path)

    def install(self, monkeypatch):
        from fulcra_media import media_config
        original = media_config.configured_media
        monkeypatch.setattr(media_config, 'configured_media',
                            lambda path, **kw: original(path, **{'client': self.client, **kw}))


@pytest.fixture
def authority(tmp_path, monkeypatch):
    # Environment restoration is essential: the helper's subprocess use has no monkeypatch.
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic')
    result = Authority(tmp_path / 'profile')
    result.install(monkeypatch)
    from fulcra_media import webhook_receiver as wr
    for name in ('normalize_jellyfin', 'normalize_plex'):
        normalize = getattr(wr, name)
        monkeypatch.setattr(wr, name, lambda payload, normalize=normalize:
                            normalize(payload, now=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    monkeypatch.setattr('fulcra_media.cli.STATE_PATH', tmp_path / 'legacy')
    return result
