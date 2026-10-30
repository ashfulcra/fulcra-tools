"""Explicitly scoped, read-only legacy exports with private raw receipts.

A transport EOF is not provider completeness. This reader never seals a ledger,
uploads records, creates datatypes or adopts ambient credentials. Its caller
supplies an already bound client and a fresh account/origin consent guard.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

import httpx

from fulcra_common.json_values import snapshot_json
from fulcra_common.record_observations import _PARTIAL_HEADERS
from fulcra_common.v1 import _canonical_uuid
from .migration_plan import MigrationPlan, _time
from .record_actions import _json

_MAX_BODY = 64 * 1024 * 1024
_MAX_ROW = 1024 * 1024
_MAX_SNAPSHOT = 16 * 1024 * 1024


def _response_metadata(headers):
    """Retain fixed names and bounded counts, never arbitrary header values."""
    def count(name):
        value = headers.get(name)
        if value is None:
            return None
        return int(value) if re.fullmatch(r'[0-9]{1,10}', value) else 'invalid'
    return {'partial_headers': [name for name in _PARTIAL_HEADERS if name in headers],
            'total_counts': [count('x-total-count'), count('x-total')],
            'content_length': count('content-length')}


def _validate_response_metadata(value, *, row_count, byte_count):
    if (not isinstance(value, dict)
            or set(value) != {'partial_headers', 'total_counts', 'content_length'}
            or value['partial_headers'] != []
            or not isinstance(value['total_counts'], list) or len(value['total_counts']) != 2):
        raise ValueError('incomplete response metadata')
    for observed, expected in [*( (v, row_count) for v in value['total_counts']),
                               (value['content_length'], byte_count)]:
        if observed is not None and (type(observed) is not int or not 0 <= observed <= 9999999999
                                     or observed != expected):
            raise ValueError('incomplete response metadata totals')


def _private(info, *, directory=False):
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError('historical export artifacts must be private owned files/directories')


@contextmanager
def _directory(path, *, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def _write(parent, name, value):
    body = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=True).encode()
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as output:
            output.write(body)
            output.flush()
            os.fsync(fd)
        os.fsync(parent)
    finally:
        os.close(fd)
    return hashlib.sha256(body).hexdigest()


def _read(parent, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private(os.fstat(fd))
        with os.fdopen(fd, 'rb', closefd=False) as source:
            body = source.read(limit + 1)
        if len(body) > limit:
            raise ValueError('historical export artifact exceeds its bound')
        return body
    finally:
        os.close(fd)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON keys')
        result[key] = value
    return result


def _float(text):
    value = float(text)
    if not math.isfinite(value) or Decimal(text) != Decimal(str(value)):
        raise ValueError('lossy JSON number')
    return value


def _invalid_constant(_):
    raise ValueError('non-finite JSON number')


def _row_end(body, start):
    """Bound one object's bytes/depth before allocating its decoded tree."""
    if body[start:start + 1] != b'{':
        raise ValueError('expected a JSON object')
    stack, quoted, escaped = [], False, False
    for index in range(start, min(len(body), start + _MAX_ROW)):
        char = body[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (123, 91):
            stack.append(125 if char == 123 else 93)
            if len(stack) > 64:
                raise ValueError('JSON nesting exceeds its bound')
        elif char in (125, 93):
            if not stack or stack.pop() != char:
                raise ValueError('JSON container mismatch')
            if not stack:
                return index + 1
    raise ValueError('JSON row exceeds its byte bound or is incomplete')


def _records(body):
    """Parse at most 4,000 bounded objects, preserving JSON multiplicity/order."""
    index, encoded, size = 0, [], 0
    def whitespace(index):
        while index < len(body) and body[index] in b' \t\r\n':
            index += 1
        return index
    index = whitespace(index)
    if body[index:index + 1] != b'[':
        raise ValueError('expected an unwrapped JSON array')
    index = whitespace(index + 1)
    if body[index:index + 1] != b']':
        while True:
            if len(encoded) >= 4000:
                raise ValueError('JSON snapshot exceeds its row bound')
            end = _row_end(body, index)
            row = snapshot_json(json.loads(body[index:end].decode('utf-8'), object_pairs_hook=_pairs,
                parse_float=_float, parse_constant=_invalid_constant))
            canonical = _json(row)
            size += len(canonical.encode())
            if size > _MAX_SNAPSHOT:
                raise ValueError('JSON snapshot exceeds its canonical byte bound')
            encoded.append(canonical)
            index = whitespace(end)
            if body[index:index + 1] == b']':
                break
            if body[index:index + 1] != b',':
                raise ValueError('JSON array separator is unreadable')
            index = whitespace(index + 1)
    if body[index:index + 1] != b']' or whitespace(index + 1) != len(body):
        raise ValueError('JSON array has trailing/incomplete data')
    return '[' + ','.join(encoded) + ']'


@dataclass(frozen=True)
class RawExport:
    body_path: Path
    receipt_path: Path
    _encoded: str = field(repr=False)
    _receipt_sha256: str = field(repr=False)
    coverage_complete: bool = field(default=False, init=False)

    @property
    def records(self):
        return json.loads(self._encoded)


class HistoricalReader:
    """Retain one finite HTTP response before parsing, without coverage claims.

    ``guard()`` must recheck current consent/client origin and return the current
    principal UUID; it runs before credential lookup, immediately before GET,
    on each raw chunk and after EOF. A new attempt directory is created for
    every GET, including retries. Partial/orphan bodies are never reused.
    Requests require identity encoding; encoded responses are retained without
    decoding and rejected. ``receipt.json`` proves a fully retained HTTP body, not a complete
    provider dataset or a later fresh artifact audit.
    """
    def __init__(self, plan, *, client, guard, artifacts, pass_id,
                 max_bytes=_MAX_BODY, timeout=30, clock=time.monotonic):
        self.plan = MigrationPlan.from_document(plan.document)
        self.client, self.guard = client, guard
        self.pass_id = _canonical_uuid(pass_id)
        if (type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BODY
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60 or not callable(clock)
                or not callable(guard)):
            raise ValueError('historical export bounds/guard are invalid')
        self.max_bytes, self.timeout, self.clock = max_bytes, timeout, clock
        self.root = Path(artifacts).absolute()
        self._guard()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scope = {'version': 2, 'pass_id': self.pass_id, 'plan_fingerprint': self.plan.fingerprint}
        with _directory(self.root) as parent:
            created = False
            try:
                os.mkdir(self.pass_id, mode=0o700, dir_fd=parent)
                created = True
                os.fsync(parent)
            except FileExistsError:
                pass
            with _directory(self.pass_id, parent=parent) as scoped:
                if created:
                    _write(scoped, 'scope.json', self._scope)
                self._check_scope(scoped)

    def _guard(self):
        if self.guard() != self.plan.account_id:
            raise ValueError('historical export account scope changed or is unknown')

    def _check_scope(self, parent):
        try:
            if json.loads(_read(parent, 'scope.json', 4096)) != self._scope:
                raise ValueError
        except (ValueError, OSError):
            raise ValueError('historical export pass scope changed or is unreadable') from None

    @staticmethod
    def _check_origin(http):
        origin = http.base_url
        if (origin.scheme != 'https' or origin.host != 'api.fulcradynamics.com'
                or origin.port not in {None, 443} or origin.path != '/' or origin.userinfo):
            raise ValueError('historical export API origin is not trusted')

    def verify_result(self, result, window_id, *, start_time, end_time, order):
        """Reopen scoped artifacts before a later actor uses their read evidence.

        Returns immutable-body hashes/count, never a completeness assertion.
        The same verified bytes must reproduce the returned snapshot. A caller
        cannot substitute another pass, account, range, sort order or body.
        """
        self._guard()
        request = self._request_info(window_id, start_time, end_time, order)
        try:
            if type(result) is not RawExport or result.coverage_complete is not False:
                raise ValueError
            relative = result.body_path.relative_to(self.root / self.pass_id)
            if len(relative.parts) != 2 or relative.parts[1] != 'body.json':
                raise ValueError
            attempt = _canonical_uuid(relative.parts[0])
            if result.receipt_path != result.body_path.parent / 'receipt.json':
                raise ValueError
            with _directory(self.root) as root, _directory(self.pass_id, parent=root) as scoped:
                self._check_scope(scoped)
                with _directory(attempt, parent=scoped) as folder:
                    request_body = _read(folder, 'request.json', 16384)
                    if json.loads(request_body, object_pairs_hook=_pairs) != request:
                        raise ValueError
                    receipt_body = _read(folder, 'receipt.json', 4096)
                    receipt_hash = hashlib.sha256(receipt_body).hexdigest()
                    receipt = json.loads(receipt_body, object_pairs_hook=_pairs)
                    body = _read(folder, 'body.json', self.max_bytes)
                    request_hash = hashlib.sha256(request_body).hexdigest()
                    body_hash = hashlib.sha256(body).hexdigest()
                    if (set(receipt) != {'version', 'request_sha256', 'body_sha256', 'bytes', 'http_status',
                                        'transport_eof', 'decoded_body', 'body_encoding', 'coverage_complete', 'response_metadata'}
                            or receipt_hash != result._receipt_sha256
                            or type(receipt['version']) is not int or receipt['version'] != 2
                            or receipt['request_sha256'] != request_hash or receipt['body_sha256'] != body_hash
                            or type(receipt['bytes']) is not int or receipt['bytes'] != len(body)
                            or type(receipt['http_status']) is not int or receipt['http_status'] != 200
                            or receipt['transport_eof'] is not True or receipt['decoded_body'] is not True
                            or receipt['body_encoding'] not in {'', 'identity'} or receipt['coverage_complete'] is not False
                            or _records(body) != result._encoded):
                        raise ValueError
                    _validate_response_metadata(receipt['response_metadata'],
                                                row_count=len(result.records), byte_count=len(body))
            self._guard()
            return {'attempt_id': attempt, 'request_sha256': request_hash, 'body_sha256': body_hash,
                    'receipt_sha256': receipt_hash,
                    'snapshot_sha256': hashlib.sha256(result._encoded.encode()).hexdigest(),
                    'count': len(result.records)}
        except (ValueError, OSError, TypeError, AttributeError, UnicodeError, RecursionError, InvalidOperation):
            raise RuntimeError('historical export artifact integrity or scope changed before consumption') from None

    def _request_info(self, window_id, start_time, end_time, order):
        _canonical_uuid(window_id)
        start, end = _time(start_time), _time(end_time)
        scope = self.plan.document
        if (start < _time(scope['start_time']) or end > _time(scope['end_time'])
                or end <= start or order not in {'asc', 'desc'}):
            raise ValueError('historical export window/order is outside the immutable plan')
        start_time, end_time = [value.isoformat().replace('+00:00', 'Z') for value in (start, end)]
        return {**self._scope, 'window_id': window_id,
            'account_id': self.plan.account_id, 'source_data_type': self.plan.source_data_type,
            'start_time': start_time, 'end_time': end_time, 'order': order,
            'max_body_bytes': self.max_bytes, 'timeout_seconds': self.timeout}

    def read(self, window_id, *, start_time, end_time, order):
        request = self._request_info(window_id, start_time, end_time, order)
        start_time, end_time = request['start_time'], request['end_time']
        self._guard()
        http = self.client._client()
        self._check_origin(http)
        headers = self.client._authed_headers()
        self._guard()
        self._check_origin(http)
        attempt = str(uuid4())
        with _directory(self.root) as parent, _directory(self.pass_id, parent=parent) as scoped:
            self._check_scope(scoped)
            os.mkdir(attempt, mode=0o700, dir_fd=scoped)
            os.fsync(scoped)
            with _directory(attempt, parent=scoped) as folder:
                request_hash = _write(folder, 'request.json', request)
                body_fd = os.open('body.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=folder)
                digest, count = hashlib.sha256(), 0
                deadline = self.clock() + self.timeout
                def check():
                    self._guard()
                    if self.clock() >= deadline:
                        raise RuntimeError('historical export deadline exceeded; response state is unknown')
                try:
                    with os.fdopen(body_fd, 'wb', closefd=False) as output:
                        try:
                            check()
                            self._check_origin(http)
                            with http.stream('GET', 'https://api.fulcradynamics.com/data/v1alpha1/event/' + self.plan.source_data_type,
                                    params={'start_time': start_time, 'end_time': end_time,
                                        'fulcra_userid': self.plan.account_id, 'format': 'json', 'sort': order},
                                    headers={**headers, 'Accept-Encoding': 'identity'}, follow_redirects=False,
                                    timeout=httpx.Timeout(self.timeout)) as response:
                                encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
                                metadata = _response_metadata(response.headers)
                                for chunk in response.iter_raw():
                                    check()
                                    retained = chunk[:self.max_bytes - count]
                                    output.write(retained)
                                    digest.update(retained)
                                    count += len(retained)
                                    if len(retained) != len(chunk):
                                        raise RuntimeError('historical export response exceeds its byte bound; preserve partial artifact')
                                check()
                                status = response.status_code
                        finally:
                            output.flush()
                            os.fsync(body_fd)
                            os.fsync(folder)
                except httpx.HTTPError:
                    raise RuntimeError('historical export transport failed; response state is unknown') from None
                finally:
                    os.close(body_fd)
                receipt_hash = _write(folder, 'receipt.json', {'version': 2, 'request_sha256': request_hash,
                    'body_sha256': digest.hexdigest(), 'bytes': count, 'http_status': status,
                    'transport_eof': True, 'decoded_body': encoding in {'', 'identity'},
                    'body_encoding': encoding if encoding in {'', 'identity', 'gzip', 'deflate', 'br', 'zstd'} else 'unsupported',
                    'coverage_complete': False, 'response_metadata': metadata})
                if status != 200:
                    raise RuntimeError('historical export HTTP status is not a complete successful read')
                if encoding not in {'', 'identity'}:
                    raise RuntimeError('historical export response encoding is unsupported; compressed raw artifact retained')
                retained_body = _read(folder, 'body.json', self.max_bytes)
                if len(retained_body) != count or hashlib.sha256(retained_body).hexdigest() != digest.hexdigest():
                    raise RuntimeError('historical export raw artifact integrity changed before parsing')
                try:
                    self._guard()
                    encoded = _records(retained_body)
                except (ValueError, TypeError, UnicodeError, InvalidOperation, RecursionError):
                    raise RuntimeError('historical export JSON is unreadable, unsupported or not lossless; raw artifact retained') from None
                try:
                    _validate_response_metadata(metadata, row_count=len(json.loads(encoded)), byte_count=count)
                except ValueError:
                    raise RuntimeError('historical export response metadata is not complete; raw artifact retained') from None
                check()
                path = self.root / self.pass_id / attempt
                return RawExport(path / 'body.json', path / 'receipt.json', encoded, receipt_hash)
