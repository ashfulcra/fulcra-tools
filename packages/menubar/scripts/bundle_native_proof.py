"""Bounded, stdlib-only origin/range proof. No native verification or acceptance.

Only little-endian Mach-O 64 (ARM64/x86_64) and big-endian FAT32 are
qualified. Signing supports an opaque terminal LC_CODE_SIGNATURE insertion or
replacement and exact controlled __LINKEDIT filesize/vmsize values. Other
changes are UNKNOWN. ``controlled_fields`` is data, never stage authority.
"""
from dataclasses import dataclass
import struct
import hashlib
import time
from bundle_operation_deadline import current_operation_deadline

MAX_PAIR_BYTES = 64 * 1024 * 1024
PAIR_SECONDS = 60
MAX_SLICES = 16
MAX_COMMANDS = 512
_THIN = b'\xcf\xfa\xed\xfe'
_FAT = b'\xca\xfe\xba\xbe'
_NATIVE_MAGICS = {_THIN, _FAT, b'\xfe\xed\xfa\xcf', b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}
_CPUS = {0x1000007, 0x100000c}
_VM_LIMIT = 1 << 64  # Exclusive end of the 64-bit virtual address space.
# Qualified opaque command classes stay byte-exact; none is a signing wildcard.
_COMMAND_MIN = {0x2: 24, 0xb: 80, 0xc: 24, 0xd: 24, 0xe: 12,
                0x19: 72, 0x1b: 24, 0x1d: 16, 0x24: 16, 0x26: 16,
                0x29: 16, 0x2a: 16, 0x32: 24, 0x80000022: 48,
                0x80000028: 24, 0x8000001c: 12}


@dataclass(frozen=True)
class LinkerDataRequirement:
    """Required source data, never privacy permission or signature authority."""
    path: str
    bytes: int
    mode: int
    sha256: str
    archive_name: str
    archive_sha256: str
    member: str


class LinkerDataUnknown(ValueError):
    """The active source data requirement is absent, ambiguous, or changed."""


# Selection uses only independently owned expected alias fields, never app Info.
_LINKER_DATA_POLICIES = (((
    'Contents/Frameworks/Python.framework',
    'Contents/Frameworks/Python.framework/Versions/3.13/Python',
    'Contents/Frameworks/Python.framework/Versions/3.13/Resources/Info.plist',
    'f4381e1bc9ed0134a026120fd580ce0d516505bb9fd046252561038244560af8',
    (('Contents/Frameworks/Python.framework/Versions/Current', '3.13'),
     ('Contents/Frameworks/Python.framework/Python', 'Versions/Current/Python'),
     ('Contents/Frameworks/Python.framework/Resources', 'Versions/Current/Resources'))),
    (LinkerDataRequirement(
        'Contents/Frameworks/Python.framework/Versions/3.13/lib/python3.13/config-3.13-darwin/python.o',
        9440, 0o644, '1243d6c664a706a1428c09c7d7db95bc77d34c77ff965eb262b46fa84e58a8b5',
        'Python-3.13-macOS-support.b10.tar.gz',
        '1028a51fc7f3f953cac832d9252c5d2577f2f0e7a03d23ce2f1a36b702b60248',
        'Python.xcframework/macos-arm64_x86_64/Python.framework/Versions/3.13/lib/python3.13/config-3.13-darwin/python.o'),)),)


@dataclass(frozen=True)
class DebugDataOrigin:
    """Complete pinned wheel member and fixed slice, not a Mach-O permission."""
    requirement: LinkerDataRequirement
    archive_bytes: int
    member_bytes: int
    member_sha256: str
    slice_offset: int


_DEBUG_DATA_SELECTOR = (_LINKER_DATA_POLICIES[0][0],
    ('.', 'Contents/MacOS/Fulcra Collect', 'Contents/Info.plist', '', ()))
_DEBUG_DATA_ORIGINS = tuple(DebugDataOrigin(
    LinkerDataRequirement('Contents/Resources/app_packages/' + member, size, 0o644, digest,
        'pyobjc_core-12.2.2-cp313-cp313-macosx_10_13_universal2.whl',
        '950bd2d9c74634398c4e3d24ef2f213d4e23d705083697464fa67afedc53c1ad', member),
    6427113, full_size, full_digest, offset)
    for member, size, digest, full_size, full_digest, offset in (
        ('objc/_machsignals.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/_machsignals.cpython-313-darwin.so',
         29114, 'e28126b48403e9b288ad02cedd628a55c4b57b56bc7ff31b02bfbb88ca8ec1c4',
         58202, '66c0fc7c1f2c3372ec613761cf6a4482a33a9e8a4f09b312c9ed31a11fa4280b', 29088),
        ('objc/_objc.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/_objc.cpython-313-darwin.so',
         3699949, '22f94e97cb8b9729df97d0ffb790ae70ae278afafa07553b4c34fbf97cf6134f',
         7267629, '4b868a052995011fa4a234b8615b8aded27327bd9bd4d02af73805a52a8ad924', 3567680)))


def requirements_from_expected_aliases(expected_aliases: tuple) -> tuple[LinkerDataRequirement, ...]:
    if type(expected_aliases) is not tuple:
        raise LinkerDataUnknown('UNKNOWN: expected source alias type')
    selected, keys = [], []
    for alias in expected_aliases:
        key = (alias.container, alias.executable, alias.info_path, alias.info_sha256, alias.links)
        keys.append(key)
        for expected, records in _LINKER_DATA_POLICIES:
            if key == expected:
                selected.extend(records)
    if all(key in keys for key in _DEBUG_DATA_SELECTOR):
        if any(keys.count(key) != 1 for key in _DEBUG_DATA_SELECTOR):
            raise LinkerDataUnknown('UNKNOWN: duplicate source data selector')
        selected.extend(origin.requirement for origin in _DEBUG_DATA_ORIGINS)
    if len({r.path for r in selected}) != len(selected):
        raise LinkerDataUnknown('UNKNOWN: duplicate source data requirement')
    return tuple(selected)


def classify_signing_input(expected_aliases: tuple, relative_path: str, raw: bytes, mode: int) -> str:
    for record in requirements_from_expected_aliases(expected_aliases):
        if relative_path == record.path:
            if (type(raw) is not bytes or type(mode) is not int or mode != record.mode
                    or len(raw) != record.bytes or hashlib.sha256(raw).hexdigest() != record.sha256):
                raise LinkerDataUnknown('UNKNOWN: required source data changed')
            return 'exact-linker-data'
    return 'ordinary'


def require_linker_data_inventory(expected_aliases: tuple, entries: tuple) -> tuple[LinkerDataRequirement, ...]:
    records = requirements_from_expected_aliases(expected_aliases)
    for record in records:
        matches = [entry for entry in entries if entry.path == record.path]
        if len(matches) != 1:
            raise LinkerDataUnknown('UNKNOWN: required source data missing or duplicate')
        entry = matches[0]
        if (entry.kind != 'file' or type(entry.mode) is not int or entry.mode != record.mode
                or type(entry.size) is not int or entry.size != record.bytes or entry.sha256 != record.sha256):
            raise LinkerDataUnknown('UNKNOWN: required source data inventory changed')
    return records


@dataclass(frozen=True)
class StructuralProof:
    """Ranges are (current_start, origin_start, length), always byte-exact.

    excluded_regions and changed_regions are (current_start, length, kind).
    discarded_regions are (origin_start, length, kind). Stable + excluded
    partition current bytes; mapped stable origins + discarded partition origin
    bytes. Discarded includes original headers/signatures and omitted slices.
    Header/signature/padding never appear in stable_ranges. No raw bytes escape.
    """
    complete: bool
    reason: str
    stable_ranges: tuple = ()
    excluded_regions: tuple = ()
    changed_regions: tuple = ()
    discarded_regions: tuple = ()
    signed_acceptance: bool = False


@dataclass(frozen=True)
class _Thin:
    cpu: int
    subtype: int
    header_end: int
    commands: tuple
    segments: tuple
    signature: tuple | None
    stable: tuple
    excluded: tuple


class _Unknown(Exception):
    pass


def _require(value, reason='unsupported native geometry'):
    if not value:
        raise _Unknown(reason)


def _tick(deadline):
    operation=current_operation_deadline()
    if operation is not None: deadline=min(deadline,operation.expires)
    _require(time.monotonic() < deadline, 'pair deadline exceeded')


def _complement(size, ranges, kind):
    result, position = [], 0
    for start, length in sorted(ranges):
        _require(start >= position and length >= 0 and start + length <= size)
        if start > position:
            result.append((position, start - position, kind))
        position = start + length
    if position < size:
        result.append((position, size - position, kind))
    return tuple(result)


def _thin(data, deadline):
    _tick(deadline)
    _require(len(data) >= 32 and data[:4] == _THIN)
    _, cpu, subtype, filetype, count, size, _, reserved = struct.unpack_from('<8I', data)
    _require(cpu in _CPUS and filetype in {2, 6, 8} and reserved == 0)
    _require(0 < count <= MAX_COMMANDS and 32 + size <= len(data))
    position, commands, segments, sections, signature, seen = 32, [], [], [], None, set()
    for _ in range(count):
        _tick(deadline)
        _require(position + 8 <= 32 + size)
        cmd, length = struct.unpack_from('<II', data, position)
        _require(cmd in _COMMAND_MIN and length >= _COMMAND_MIN[cmd] and length % 8 == 0)
        _require(position + length <= 32 + size)
        # Dylib/RPATH commands may repeat; all other command identities are unique.
        if cmd not in {0x19, 0xc, 0x8000001c}:
            _require(cmd not in seen, 'duplicate native command')
            seen.add(cmd)
        commands.append((cmd, position, length))
        if cmd == 0x19:
            _, _, name, vmaddr, vmsize, offset, extent, _, _, nsects, _ = struct.unpack_from('<II16sQQQQiiII', data, position)
            _require(length == 72 + nsects * 80 and nsects <= MAX_COMMANDS)
            _require(offset <= len(data) and offset + extent <= len(data))
            _require(vmaddr + vmsize <= _VM_LIMIT)
            _require(extent <= vmsize and not any(s[0] == name for s in segments))
            segments.append((name, offset, extent, vmaddr, vmsize, position))
            for index in range(nsects):
                base = position + 72 + 80 * index
                section_name, segment_name, addr, secsize, segoff, align, reloff, nreloc, flags, _, _, _ = struct.unpack_from('<16s16sQQ8I', data, base)
                _require(segment_name == name and align <= 31)
                _require(addr + secsize <= _VM_LIMIT)
                _require(vmaddr <= addr and addr + secsize <= vmaddr + vmsize)
                _require(reloff + nreloc * 8 <= len(data))
                if flags & 0xff not in {1, 0xc, 0x12} and secsize:
                    _require(offset <= segoff and segoff + secsize <= offset + extent and segoff >= 32 + size)
                    sections.append((segoff, secsize))
        elif cmd == 0x1d:
            _require(length == 16 and signature is None)
            sigoff, sigsize = struct.unpack_from('<II', data, position + 8)
            _require(sigsize > 0 and sigoff >= 32 + size and sigoff + sigsize == len(data))
            signature = (sigoff, sigsize, position)
        position += length
    _require(position == 32 + size and segments)
    occupied = sorted((s[1], s[2]) for s in segments if s[2])
    _require(occupied)
    _complement(len(data), occupied, 'padding')  # checks overlapping file segments
    virtual = sorted((s[3], s[4]) for s in segments if s[4])
    for (start, extent), (next_start, _) in zip(virtual, virtual[1:]):
        _require(start + extent <= next_start)
    _complement(len(data), sections, 'unused')  # checks overlapping sections
    _require(occupied[0][0] == 0 and occupied[0][1] >= 32 + size)
    _require(max(start + extent for start, extent in occupied) == len(data), 'unaccounted native suffix')
    link = [s for s in segments if s[0].rstrip(b'\0') == b'__LINKEDIT']
    if signature:
        _require(len(link) == 1 and link[0][1] <= signature[0] and link[0][1] + link[0][2] == len(data))
        _require(not any(start < len(data) and start + extent > signature[0] for start, extent in sections))
    stable, excluded = [], []
    for start, extent in occupied:
        end = start + extent
        if start == 0:
            body_start = min((s[0] for s in sections if s[0] < end), default=32 + size)
            body_start = max(32 + size, body_start)
            # Header slack is untrusted, even in command-only synthetic layouts.
            while body_start < end and data[body_start] == 0:
                if body_start % 65536 == 0:
                    _tick(deadline)
                body_start += 1
            excluded.append((0, body_start, 'header'))
            start = body_start
        if signature and start <= signature[0] < end:
            end = signature[0]
        if end > start:
            stable.append((start, end - start))
    for start, length, _ in _complement(len(data), occupied, 'padding'):
        _require(not any(data[start:start + length]), 'nonzero native padding')
        excluded.append((start, length, 'padding'))
    if signature:
        excluded.append((signature[0], signature[1], 'signature'))
    return _Thin(cpu, subtype, 32 + size, tuple(commands), tuple(segments), signature, tuple(stable), tuple(sorted(excluded)))


def _layout(data, deadline):
    _tick(deadline)
    if data[:4] == _THIN:
        return ((0, len(data), _thin(data, deadline)),), ()
    _require(data[:4] == _FAT and len(data) >= 8)
    count = struct.unpack_from('>I', data, 4)[0]
    _require(0 < count <= MAX_SLICES and 8 + 20 * count <= len(data))
    rows, cpus = [], set()
    for i in range(count):
        _tick(deadline)
        cpu, subtype, offset, size, align = struct.unpack_from('>IIIII', data, 8 + 20 * i)
        _require(cpu in _CPUS and cpu not in cpus, 'duplicate or unsupported FAT architecture')
        cpus.add(cpu)
        _require(align <= 31 and offset % (1 << align) == 0 and offset >= 8 + 20 * count and size > 0 and offset + size <= len(data))
        thin = _thin(data[offset:offset + size], deadline)
        _require((thin.cpu, thin.subtype) == (cpu, subtype))
        rows.append((offset, size, thin))
    ranges = [(offset, size) for offset, size, _ in rows]
    padding = _complement(len(data), ranges, 'padding')
    _require(max(offset + size for offset, size in ranges) == len(data), 'unaccounted FAT suffix')
    for start, size, _ in padding:
        if start == 0:
            start = 8 + 20 * count
            size = min(offset for offset, _, _ in rows) - start
        _require(not any(data[start:start + size]), 'nonzero discarded FAT padding')
    # FAT header and gaps cannot receive public permission.
    return tuple(rows), _complement(len(data), ranges, 'fat-header-padding')


def _signed_pair(origin, current, old, new, controlled_fields):
    _require(old.cpu == new.cpu and old.subtype == new.subtype and new.signature is not None)
    fields = controlled_fields
    _require(isinstance(fields, dict) and set(fields) == {'linkedit_vmsize', 'linkedit_filesize'}, 'missing or unsupported controlled field')
    _require(all(type(value) is int and value >= 0 for value in fields.values()))
    old_non = [c for c in old.commands if c[0] != 0x1d]
    new_non = [c for c in new.commands if c[0] != 0x1d]
    _require(len(old_non) == len(new_non))
    old_end = old.signature[0] if old.signature else len(origin)
    _require(new.signature[0] == old_end, 'signature moved over payload')
    expected_header = bytearray(origin[:old.header_end])
    if old.signature:
        _require(new.header_end == old.header_end and old.signature[2] == new.signature[2])
    else:
        _require(new.header_end == old.header_end + 16 and new.signature[2] == old.header_end)
        _require(origin[old.header_end:new.header_end] == b'\0' * 16, 'signature command needs zero slack')
        expected_header.extend(current[old.header_end:new.header_end])
        struct.pack_into('<II', expected_header, 16, len(new.commands), new.header_end - 32)
    _require(len(old.segments) == len(new.segments))
    for a, b in zip(old_non, new_non):
        _require(a == b)
    for a, b in zip(old.segments, new.segments):
        _require(a[0] == b[0] and a[1] == b[1] and a[3] == b[3] and a[5] == b[5])
        if a[0].rstrip(b'\0') == b'__LINKEDIT':
            _require(b[2] == fields['linkedit_filesize'] and b[4] == fields['linkedit_vmsize'])
            _require(b[1] + b[2] == len(current) and a[1] + a[2] == len(origin))
            struct.pack_into('<Q', expected_header, b[5] + 32, b[4])
            struct.pack_into('<Q', expected_header, b[5] + 48, b[2])
        else:
            _require(a == b)
    if old.signature:
        expected_header[new.signature[2]:new.signature[2] + 16] = current[new.signature[2]:new.signature[2] + 16]
    _require(bytes(expected_header) == current[:new.header_end], 'unsupported header change')
    _require(origin[new.header_end:old_end] == current[new.header_end:old_end], 'non-signing payload changed')
    return ((0, new.header_end, 'signing-header'), (new.signature[0], new.signature[1], 'opaque-signature'))


def prove_public_pair(origin: bytes, current: bytes, transform: str, *, controlled_fields: dict | None = None) -> StructuralProof:
    """Prove exact origin mapping or a narrowly supported signing-field change.

    Even complete=True confers no transformed-stage or native acceptance.
    Invalid/unsupported input returns complete=False with no usable intervals.
    """
    deadline = time.monotonic() + PAIR_SECONDS
    try:
        _require(transform in {'raw', 'postbuild', 'signed'}, 'unsupported transform')
        _require(type(origin) is bytes and type(current) is bytes and max(len(origin), len(current)) <= MAX_PAIR_BYTES, 'pair size/type limit')
        _tick(deadline)
        native = origin[:4] in _NATIVE_MAGICS or current[:4] in _NATIVE_MAGICS
        if not native:
            _require(origin == current, 'non-native bytes changed')
            _tick(deadline)
            return StructuralProof(True, 'exact public bytes', ((0, 0, len(current)),) if current else ())
        old_rows, _ = _layout(origin, deadline)
        new_rows, outside = _layout(current, deadline)
        if origin == current:
            mapping = [(row, row) for row in old_rows]
        elif current[:4] == _THIN and origin[:4] == _FAT:
            candidates = [r for r in old_rows if (r[2].cpu, r[2].subtype) == (new_rows[0][2].cpu, new_rows[0][2].subtype)]
            _require(len(candidates) == 1)
            mapping = [(candidates[0], new_rows[0])]
        else:
            _require(len(old_rows) == len(new_rows) == 1 and origin[:4] == current[:4] == _THIN, 'unsupported changed FAT layout')
            mapping = [(old_rows[0], new_rows[0])]
        stable, excluded, changed = [], list(outside), []
        for (oldoff, oldsize, old), (newoff, newsize, new) in mapping:
            _tick(deadline)
            a, b = origin[oldoff:oldoff + oldsize], current[newoff:newoff + newsize]
            if a != b:
                _require(transform != 'raw', 'raw bytes changed')
                changes = _signed_pair(a, b, old, new, controlled_fields)
                changed.extend((newoff + start, size, kind) for start, size, kind in changes)
            stable.extend((newoff + start, oldoff + start, size) for start, size in new.stable)
            excluded.extend((newoff + start, size, kind) for start, size, kind in new.excluded)
        merged = []
        for start, oldstart, size in sorted(stable):
            if merged and merged[-1][0] + merged[-1][2] == start and merged[-1][1] + merged[-1][2] == oldstart:
                a, b, n = merged[-1]
                merged[-1] = (a, b, n + size)
            else:
                merged.append((start, oldstart, size))
        stable = merged
        for start, oldstart, size in stable:
            _require(current[start:start + size] == origin[oldstart:oldstart + size], 'stable byte mismatch')
        _tick(deadline)
        # Current stable/excluded intervals must partition every byte exactly.
        _require(not _complement(len(current), [(s, n) for s, _, n in stable] + [(s, n) for s, n, _ in excluded], 'unaccounted'))
        discarded = _complement(len(origin), [(b, n) for _, b, n in stable], 'untrusted-or-discarded-origin')
        return StructuralProof(True, 'structural public mapping only', tuple(stable), tuple(sorted(excluded)), tuple(changed), discarded)
    except (_Unknown, struct.error, OverflowError, ValueError, TypeError):
        return StructuralProof(False, 'UNKNOWN: unsupported, changed, malformed, or bounded-out pair')
