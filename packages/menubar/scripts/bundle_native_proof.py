"""Bounded, stdlib-only origin/range proof. No native verification or acceptance.

Only little-endian Mach-O 64 (ARM64/x86_64) and big-endian FAT32 are
qualified. Signing supports an opaque terminal LC_CODE_SIGNATURE insertion or
replacement and exact controlled __LINKEDIT filesize/vmsize values. Other
changes are UNKNOWN. ``controlled_fields`` is data, never stage authority.
"""
from dataclasses import dataclass
import struct
import time

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
