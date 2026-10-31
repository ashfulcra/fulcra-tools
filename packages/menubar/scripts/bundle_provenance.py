"""Pure exact public-input verification and bounded bundle privacy scanning.

The fixed source catalog is never supplied by a request. All selected inputs
must verify even when no marker occurs. This component performs no
cleanup, command execution, download, native verification, or stage adoption.
"""
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import time
from bundle_operation_deadline import current_operation_deadline, OperationDeadlineExceeded
import tomllib
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile
import zlib

from bundle_native_proof import prove_public_pair

_CATALOG_PATH = Path(__file__).with_name('public_bundle_inputs.json')
MAX_CATALOG_BYTES = 256 * 1024
MAX_CATALOG_RECORDS = 64
MAX_ARCHIVES = 16
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL = 256 * 1024 * 1024
MAX_ZIP_ENTRIES = 20000
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBER_TOTAL = 128 * 1024 * 1024
MAX_APP_ENTRIES = 20000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DEPTH = 128
MAX_PATH_BYTES = 4096
SCAN_SECONDS = 60
_INPUT_AUTHORITY = object()
_STAGE_AUTHORITY = object()


@dataclass(frozen=True)
class PublicMember:
    destination: str
    archive: str
    member: str
    sha256: str
    forms: tuple[str, ...]
    origin: bytes = field(repr=False)


@dataclass(frozen=True)
class VerifiedPublicInputs:
    """Only a successful loader result carries in-process origin authority.

    Member bytes are immutable and excluded from repr. No serialization here
    establishes stage authority; Task 2 must bind exact controlled operations.
    """
    complete: bool
    reason: str
    architecture: str = ''
    catalog_sha256: str = ''
    lock_sha256: str = ''
    members: tuple[PublicMember, ...] = ()
    archive_hashes: tuple[tuple[str, str], ...] = ()
    _authority: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ScanResult:
    """COMPLETE is read coverage, not privacy approval or app acceptance.

    privacy_rejections counts overlapping unpermitted marker occurrences.
    A catalog-bound file whose origin does not match also adds a rejection,
    including marker-free tampering. UNKNOWN can retain partial counts.
    """
    status: str
    reason: str
    privacy_rejections: int = 0
    permitted_hits: int = 0
    entries: int = 0
    files: int = 0
    symlinks: int = 0
    bytes_read: int = 0
    application_acceptance: bool = False
    signed_acceptance: bool = False


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    kind: str
    mode: int
    size: int = 0
    sha256: str = ''
    link: str = ''


@dataclass(frozen=True)
class _StagePermission:
    """Process-local handoff from freshly validated invocation stages only."""
    app: str
    phase: str
    entries: tuple[InventoryEntry, ...]
    controlled_fields: tuple = ()
    _authority: object = field(default=None, repr=False, compare=False)


def _stage_permission(app, phase, entries, controlled_fields=()):
    return _StagePermission(str(Path(app).absolute()), phase, entries, controlled_fields, _STAGE_AUTHORITY)


class _Unknown(Exception):
    pass


def _require(value):
    if not value:
        raise _Unknown()


def _tick(deadline):
    _require(time.monotonic() < deadline)
    operation=current_operation_deadline()
    if operation is not None:
        try: operation.check()
        except OperationDeadlineExceeded as exc: raise _Unknown() from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path):
    """Descriptor-walk every ancestor: a substituted ancestor cannot redirect us."""
    _tick(float('inf'))
    _require('..' not in Path(path).parts)
    absolute = Path(os.path.abspath(path))
    _require(len(os.fsencode(absolute)) <= MAX_PATH_BYTES)
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            _tick(float('inf'))
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nextfd
        _tick(float('inf'))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(parentfd, name, limit, deadline):
    operation=current_operation_deadline()
    if operation is not None: deadline=min(deadline,operation.expires)
    _tick(deadline)
    before = os.stat(name, dir_fd=parentfd, follow_symlinks=False)
    _require(stat.S_ISREG(before.st_mode) and before.st_size <= limit)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parentfd)
    try:
        first = os.fstat(fd)
        _require(_identity(before) == _identity(first))
        chunks, count = [], 0
        while True:
            _tick(deadline)
            chunk = os.read(fd, min(1024 * 1024, limit - count + 1))
            _tick(deadline)
            if not chunk:
                break
            count += len(chunk)
            _require(count <= limit)
            chunks.append(chunk)
        _require(count == first.st_size)
        _require(_identity(first) == _identity(os.fstat(fd)))
        _require(_identity(first) == _identity(os.stat(name, dir_fd=parentfd, follow_symlinks=False)))
        raw=b''.join(chunks)
        _tick(deadline)
        return raw
    finally:
        os.close(fd)


def _read_path(path, limit, deadline):
    _require('..' not in Path(path).parts)
    path = Path(os.path.abspath(path))
    fd = _open_directory(path.parent)
    try:
        return _read_at(fd, path.name, limit, deadline)
    finally:
        os.close(fd)


def _safe_path(value, *, directory=False):
    _require(isinstance(value, str) and value and '\\' not in value and '\0' not in value)
    _require(len(value.encode('utf-8')) <= MAX_PATH_BYTES and not value.startswith('/'))
    parts = value.rstrip('/').split('/') if directory else value.split('/')
    _require(all(part and part not in {'.', '..'} for part in parts))
    _require(directory or not value.endswith('/'))
    _require(str(PurePosixPath(value)) == value.rstrip('/'))
    return unicodedata.normalize('NFC', value.rstrip('/')).casefold()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _number(value, limit):
    _require(type(value) is int and 0 <= value <= limit)


def _wheel_lock(record, lock):
    name = record['name']
    parts = name[:-4].split('-')
    _require(name.endswith('.whl') and len(parts) in {5, 6})
    _require('-'.join(parts[-3:]) == record['tag'])
    _require(parts[-3:-1] == ['cp313', 'cp313'])
    _require(parts[-1] in {'macosx_11_0_arm64', 'macosx_10_13_universal2'})
    normalize = lambda value: value.lower().replace('_', '-').replace('.', '-')
    _require(normalize(parts[0]) == normalize(record['package']) and parts[1] == record['version'])
    _require(unquote(urlsplit(record['url']).path.rsplit('/', 1)[-1]) == name)
    packages = [p for p in lock.get('package', []) if normalize(p.get('name', '')) == normalize(record['package']) and p.get('version') == record['version']]
    _require(len(packages) == 1)
    matches = [w for w in packages[0].get('wheels', []) if w.get('url') == record['url']]
    _require(len(matches) == 1 and matches[0].get('hash') == 'sha256:' + record['sha256'] and matches[0].get('size') == record['bytes'])


def _zip_preflight(raw, deadline):
    """Bound classic single-disk central entries before ZipFile builds its index.

    ZIP64, multi-disk, and trailing container forms stay unsupported.
    A dishonest EOCD count cannot hide additional directory records.
    """
    _tick(deadline)
    offset = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    _require(offset >= 0 and offset + 22 <= len(raw))
    _, disk, directory_disk, disk_count, count, size, start, comment = struct.unpack_from('<4s4H2IH', raw, offset)
    _require(disk == directory_disk == 0 and disk_count == count)
    _require(count != 65535 and 0 < count <= MAX_ZIP_ENTRIES)
    _require(start != 0xffffffff and size != 0xffffffff)
    _require(start + size == offset and offset + 22 + comment == len(raw))
    position = start
    for _ in range(count):
        _tick(deadline)
        _require(position + 46 <= offset and raw[position:position + 4] == b'PK\x01\x02')
        name, extra, note = struct.unpack_from('<3H', raw, position + 28)
        position += 46 + name + extra + note
        _require(position <= offset)
    _require(position == offset)


def _member_bytes(raw, info, start, deadline):
    """Check actual decoder EOF/output, not ZipExtFile's declared-size truncation."""
    chunks, count, crc = [], 0, 0
    decoder = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None
    end = start + info.compress_size
    position = start
    while position < end:
        _tick(deadline)
        pending = raw[position:min(position + 65536, end)]
        position += len(pending)
        while pending:
            _tick(deadline)
            if decoder:
                output = decoder.decompress(pending, min(1024 * 1024, MAX_MEMBER_BYTES - count + 1, info.file_size - count + 1))
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    _require(not decoder.unused_data and not pending and position == end)
            else:
                output, pending = pending, b''
            count += len(output)
            _require(count <= MAX_MEMBER_BYTES and count <= info.file_size)
            crc = zlib.crc32(output, crc)
            chunks.append(output)
    _require(decoder is None or decoder.eof)
    _require(count == info.file_size and crc == info.CRC)
    return b''.join(chunks)


def load_public_inputs(archives: dict[str, Path], *, lock: Path, architecture: str, expected_catalog_sha256: str) -> VerifiedPublicInputs:
    """Verify the fixed catalog, selected exact archives, lock entries and members.

    Failures are sanitized UNKNOWN results, never an empty verified allowlist.
    Reads are no-follow/stable, ZIP decompression is streamed and output-capped.
    """
    deadline = time.monotonic() + SCAN_SECONDS
    operation=current_operation_deadline()
    if operation is not None: deadline=min(deadline,operation.expires)
    try:
        _require(_hex(expected_catalog_sha256))
        catalog_raw = _read_path(_CATALOG_PATH, MAX_CATALOG_BYTES, deadline)
        _require(_digest(catalog_raw) == expected_catalog_sha256)
        catalog = json.loads(catalog_raw, object_pairs_hook=_unique_json)
        _require(set(catalog) == {'schema', 'architecture', 'archives', 'members'})
        _require(catalog['schema'] == 1 and architecture == catalog['architecture'] == 'arm64')
        records, members = catalog['archives'], catalog['members']
        _require(isinstance(records, list) and isinstance(members, list))
        _require(0 < len(records) <= MAX_ARCHIVES and 0 < len(members) and len(records) + len(members) <= MAX_CATALOG_RECORDS)
        _require(isinstance(archives, dict) and set(archives) == {r['name'] for r in records} and len(records) == len(archives))
        lock_raw = _read_path(lock, 16 * 1024 * 1024, deadline)
        lock_data = tomllib.loads(lock_raw.decode('utf-8'))
        destinations, source_keys = set(), set()
        by_archive = {r['name']: [] for r in records}
        for member in members:
            _require(set(member) == {'destination', 'archive', 'member', 'bytes', 'sha256', 'forms'})
            key = _safe_path(member['destination'])
            _require(key not in destinations and member['destination'].startswith('Contents/'))
            destinations.add(key)
            member_key = _safe_path(member['member'])
            source_key = (member['archive'], member_key)
            _require(source_key not in source_keys and member['archive'] in by_archive)
            source_keys.add(source_key)
            _number(member['bytes'], MAX_MEMBER_BYTES)
            _require(_hex(member['sha256']))
            _require(member['forms'] in [['exact'], ['exact', 'arm64-slice']])
            by_archive[member['archive']].append(member)
        selected, hashes, compressed_total, decompressed_total = [], [], 0, 0
        seen_archive_names = set()
        for record in records:
            _tick(deadline)
            expected_keys = {'name', 'kind', 'url', 'bytes', 'sha256'}
            if record['kind'] == 'wheel':
                expected_keys |= {'package', 'version', 'tag'}
            _require(set(record) == expected_keys and record['kind'] in {'wheel', 'stub'})
            _require(_safe_path(record['name']) not in seen_archive_names and '/' not in record['name'])
            seen_archive_names.add(_safe_path(record['name']))
            _number(record['bytes'], MAX_ARCHIVE_BYTES)
            _require(_hex(record['sha256']) and by_archive[record['name']])
            if record['kind'] == 'wheel':
                _wheel_lock(record, lock_data)
            else:
                _require(record['name'] == 'GUI-Stub-3.13-b12.zip' and record['url'] == 'https://briefcase-support.s3.amazonaws.com/python/3.13/macOS/GUI-Stub-3.13-b12.zip')
                _require(record['sha256'] == '969f71ee85e2320587d8f5da486144eb1509d2d03192db477f09370e8668367d' and record['bytes'] == 15794)
            compressed_total += record['bytes']
            _require(compressed_total <= MAX_ARCHIVE_TOTAL)
            raw = _read_path(archives[record['name']], MAX_ARCHIVE_BYTES, deadline)
            _require(len(raw) == record['bytes'] and _digest(raw) == record['sha256'])
            hashes.append((record['name'], record['sha256']))
            _zip_preflight(raw, deadline)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                _require(len(infos) <= MAX_ZIP_ENTRIES)
                names, lookup, data_offsets, extents = set(), {}, {}, []
                for info in infos:
                    _tick(deadline)
                    _require(info.filename == info.orig_filename)
                    # Central-directory metadata must agree with the local header,
                    # including encryption flags on unselected members.
                    offset = info.header_offset
                    _require(offset >= 0 and offset + 30 <= archive.start_dir)
                    header = struct.unpack_from('<4s5H3I2H', raw, offset)
                    _, _, flags, method, _, _, local_crc, local_compressed, local_size, name_length, extra_length = header
                    _require(header[0] == b'PK\x03\x04' and flags == info.flag_bits and method == info.compress_type)
                    # Descriptor/ZIP64 local forms are unqualified; ordinary local
                    # CRC and sizes must describe the same bytes as the directory.
                    _require(not flags & 8)
                    _require((local_crc, local_compressed, local_size) == (info.CRC, info.compress_size, info.file_size))
                    name_start = offset + 30
                    data_start = name_start + name_length + extra_length
                    _require(data_start + info.compress_size <= archive.start_dir)
                    data_offsets[info.filename] = data_start
                    extents.append((offset, data_start + info.compress_size))
                    local_name = raw[name_start:name_start + name_length].decode('utf-8' if flags & 0x800 else 'cp437')
                    _require(local_name == info.orig_filename)
                    key = _safe_path(info.filename, directory=info.is_dir())
                    _require(key not in names and not info.flag_bits & 1)
                    names.add(key)
                    mode = info.external_attr >> 16
                    filetype = stat.S_IFMT(mode)
                    _require(filetype in {0, stat.S_IFREG, stat.S_IFDIR} and not stat.S_ISLNK(mode))
                    _require(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
                    lookup[info.filename] = info
                for previous, following in zip(sorted(extents), sorted(extents)[1:]):
                    _require(previous[1] <= following[0])
                for member in by_archive[record['name']]:
                    _tick(deadline)
                    info = lookup.get(member['member'])
                    _require(info is not None and not info.is_dir() and info.file_size == member['bytes'] and info.file_size <= MAX_MEMBER_BYTES)
                    decompressed_total += info.file_size
                    _require(decompressed_total <= MAX_MEMBER_TOTAL)
                    data = _member_bytes(raw, info, data_offsets[info.filename], deadline)
                    _require(len(data) == member['bytes'] and _digest(data) == member['sha256'])
                    selected.append(PublicMember(member['destination'], record['name'], member['member'], member['sha256'], tuple(member['forms']), data))
        _tick(deadline)
        return VerifiedPublicInputs(True, 'exact selected public inputs', architecture, _digest(catalog_raw), _digest(lock_raw), tuple(selected), tuple(hashes), _INPUT_AUTHORITY)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, EOFError, UnicodeError, zipfile.BadZipFile, zlib.error, struct.error, RecursionError, NotImplementedError, RuntimeError, _Unknown):
        return VerifiedPublicInputs(False, 'UNKNOWN: public inputs missing, changed, unsupported, or incomplete')


def _patterns(markers):
    _require(isinstance(markers, tuple) and all(isinstance(m, str) and m for m in markers))
    return tuple(dict.fromkeys(marker.encode(encoding) for marker in markers for encoding in ('utf-8', 'utf-16-le', 'utf-16-be')))


def _hits(data, patterns, deadline):
    for pattern in patterns:
        _tick(deadline)
        position = data.find(pattern)
        while position >= 0:
            _tick(deadline)
            yield position, len(pattern)
            position = data.find(pattern, position + 1)


def _validate_framework_parent_file_aliases(entries: tuple[InventoryEntry, ...], deadline: float) -> None:
    """Qualify parent-component file aliases against ordinary inventory only."""
    inventory = {}
    for entry in entries:
        _tick(deadline)
        inventory[entry.path] = entry
    for entry in entries:
        _tick(deadline)
        if entry.kind != 'link' or '..' not in entry.link.split('/'):
            continue
        _require(len(os.fsencode(entry.link)) <= MAX_PATH_BYTES)
        components = entry.link.split('/')
        _require(not os.path.isabs(entry.link) and all(p not in {'', '.'} for p in components))
        route = entry.path.split('/')[:-1]
        frameworks = [i + 1 for i, part in enumerate(route) if part.endswith('.framework')]
        _require(bool(frameworks))
        boundary = frameworks[-1]
        for index, component in enumerate(components):
            _tick(deadline)
            if component == '..':
                # Never escape and re-enter, even if lexical normalization would
                # land inside. Every earlier component was already proved a dir.
                _require(len(route) > boundary)
                route.pop()
            else:
                route.append(component)
                relative = '/'.join(route)
                _require(len(os.fsencode(relative)) <= MAX_PATH_BYTES)
                _require(len(route) <= MAX_DEPTH + 1)
                target = inventory.get(relative)
                # A canceled prefix still must exist as an ordinary directory;
                # no aliases (including the final leaf) are ever followed.
                kind = 'file' if index == len(components) - 1 else 'directory'
                _require(target is not None and target.kind == kind)
                if kind == 'directory':
                    _require(len(route) <= MAX_DEPTH)
        target = inventory.get('/'.join(route))
        _require(target is not None and target.kind == 'file')


def scan_bundle(app: Path, markers: tuple[str, ...], *, public_inputs: VerifiedPublicInputs | None = None, phase: str = 'raw', _stage=None, _inventory=None) -> ScanResult:
    """Scan every name/link/current byte with destination-bound raw permission.

    Framework links are inventoried as links; targets are never traversed. Other
    directory aliases make the scan UNKNOWN. A transformed phase needs a private
    stage-module handoff after exact inventory and fresh authority validation.
    The internal inventory collector shares this one walker/privacy predicate.
    """
    deadline = time.monotonic() + SCAN_SECONDS
    operation=current_operation_deadline()
    if operation is not None: deadline=min(deadline,operation.expires)
    counts = {'privacy_rejections': 0, 'permitted_hits': 0, 'entries': 0, 'files': 0, 'symlinks': 0, 'bytes_read': 0}
    rootfd = None
    try:
        _require(phase in {'raw', 'postbuild', 'signed'})
        if _stage is not None:
            _require(isinstance(_stage, _StagePermission) and _stage._authority is _STAGE_AUTHORITY)
            _require(_stage.app == str(Path(app).absolute()) and _stage.phase == phase)
        else:
            _require(phase == 'raw')
        collected = []
        if public_inputs is not None:
            _require(isinstance(public_inputs, VerifiedPublicInputs) and public_inputs.complete and public_inputs._authority is _INPUT_AUTHORITY)
        patterns = _patterns(markers)
        allowed = {m.destination: m for m in public_inputs.members} if public_inputs else {}
        if phase != 'raw' and 'Contents/MacOS/Stub' in allowed:
            allowed['Contents/MacOS/Fulcra Collect'] = allowed.pop('Contents/MacOS/Stub')
        controlled = dict(_stage.controlled_fields) if _stage else {}
        seen_bound, seen_names = set(), set()
        observed = {}
        rootfd = _open_directory(app)
        root_identity = _identity(os.fstat(rootfd))
        collected.append(InventoryEntry('.', 'directory', stat.S_IMODE(os.fstat(rootfd).st_mode)))
        _tick(deadline)
        # Include the root name without disclosing it in any result.
        counts['privacy_rejections'] += sum(1 for _ in _hits(os.fsencode(Path(app).name), patterns, deadline))

        def walk(fd, prefix, depth):
            _tick(deadline)
            _require(depth <= MAX_DEPTH)
            before = os.fstat(fd)
            with os.scandir(fd) as iterator:
                names = []
                for entry in iterator:
                    _tick(deadline)
                    counts['entries'] += 1
                    _require(counts['entries'] <= MAX_APP_ENTRIES)
                    names.append(entry.name)
            for name in names:
                _tick(deadline)
                relative = prefix + name
                _require(len(os.fsencode(relative)) <= MAX_PATH_BYTES)
                key = _safe_path(relative)
                _require(key not in seen_names)
                seen_names.add(key)
                counts['privacy_rejections'] += sum(1 for _ in _hits(os.fsencode(relative), patterns, deadline))
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                observed[relative] = _identity(info)
                if stat.S_ISLNK(info.st_mode):
                    counts['symlinks'] += 1
                    target = os.readlink(name, dir_fd=fd)
                    collected.append(InventoryEntry(relative, 'link', stat.S_IMODE(info.st_mode), link=target))
                    counts['privacy_rejections'] += sum(1 for _ in _hits(os.fsencode(target), patterns, deadline))
                    _require(_identity(info) == _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)))
                    # Parent-component aliases require the completed ordinary
                    # inventory below; simple framework aliases stay unchanged.
                    _require(not os.path.isabs(target) and all(p not in {'', '.'} for p in target.split('/')))
                    _require('Versions/' in relative or '.framework/' in relative)
                    _require(relative not in allowed)
                elif stat.S_ISDIR(info.st_mode):
                    collected.append(InventoryEntry(relative, 'directory', stat.S_IMODE(info.st_mode)))
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        _require(_identity(info) == _identity(os.fstat(child)))
                        walk(child, relative + '/', depth + 1)
                        _require(_identity(info) == _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)))
                    finally:
                        os.close(child)
                else:
                    _require(stat.S_ISREG(info.st_mode))
                    remaining = MAX_TOTAL_BYTES - counts['bytes_read']
                    data = _read_at(fd, name, min(MAX_FILE_BYTES, remaining), deadline)
                    counts['files'] += 1
                    counts['bytes_read'] += len(data)
                    collected.append(InventoryEntry(relative, 'file', stat.S_IMODE(info.st_mode), len(data), _digest(data)))
                    member = allowed.get(relative)
                    ranges = ()
                    if member is not None:
                        seen_bound.add(relative)
                        proof = prove_public_pair(member.origin, data, phase, controlled_fields=controlled.get(relative))
                        exact = data == member.origin
                        slice_allowed = 'arm64-slice' in member.forms and data[:4] == b'\xcf\xfa\xed\xfe' and len(data) >= 8 and int.from_bytes(data[4:8], 'little') == 0x100000c
                        if proof.complete and (phase != 'raw' or exact or slice_allowed):
                            ranges = proof.stable_ranges
                        else:
                            counts['privacy_rejections'] += 1
                    for start, length in _hits(data, patterns, deadline):
                        # Whole hit in one contiguous unchanged interval, no union.
                        permitted = any(a <= start and start + length <= a + n and data[start:start + length] == member.origin[b + start - a:b + start - a + length] for a, b, n in ranges) if member else False
                        counts['permitted_hits' if permitted else 'privacy_rejections'] += 1
            _require(_identity(before) == _identity(os.fstat(fd)))

        walk(rootfd, '', 0)
        _require(seen_bound == set(allowed))
        entries = tuple(sorted(collected, key=lambda entry: entry.path))
        _validate_framework_parent_file_aliases(entries, deadline)
        # Re-list and compare every identity, including nested directories. A file
        # read early must not change while a later sibling is being scanned.
        verified_names = set()
        def verify_inventory(fd, prefix, depth):
            _tick(deadline)
            _require(depth <= MAX_DEPTH)
            before = os.fstat(fd)
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    _tick(deadline)
                    relative = prefix + entry.name
                    _require(relative in observed and relative not in verified_names)
                    verified_names.add(relative)
                    _require(len(verified_names) <= MAX_APP_ENTRIES)
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    _require(_identity(info) == observed[relative])
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            _require(_identity(os.fstat(child)) == observed[relative])
                            verify_inventory(child, relative + '/', depth + 1)
                        finally:
                            os.close(child)
            _require(_identity(before) == _identity(os.fstat(fd)))
        verify_inventory(rootfd, '', 0)
        _require(verified_names == set(observed))
        _require(root_identity == _identity(os.fstat(rootfd)))
        # Detect root/ancestor substitution after descriptor-based traversal.
        finalfd = _open_directory(app)
        try:
            _require(root_identity == _identity(os.fstat(finalfd)))
        finally:
            os.close(finalfd)
        _tick(deadline)
        if _stage is not None:
            _require(entries == _stage.entries)
        if _inventory is not None:
            _inventory.extend(entries)
        return ScanResult('COMPLETE', 'complete raw privacy scan only' if phase == 'raw' else 'complete transformed privacy scan only', **counts)
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, _Unknown):
        return ScanResult('UNKNOWN', 'UNKNOWN: scope, authority, read, or scan bound incomplete', **counts)
    finally:
        if rootfd is not None:
            os.close(rootfd)
