"""Pure public byte mapping: no native tools, credentials, or acceptance."""
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'


def module():
    name = 'bundle_native_proof'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
        value = importlib.util.module_from_spec(spec)
        sys.modules[name] = value
        spec.loader.exec_module(value)
    return sys.modules[name]


def thin(payload=b'public marker bytes', *, cpu=0x100000c, signature=b''):
    # One __TEXT segment and optional __LINKEDIT signature at EOF.
    ncmd = 3 if signature else 1
    sizeof = 72 + (72 + 16 if signature else 0)
    start = 32 + sizeof
    end = start + len(payload)
    header = struct.pack('<8I', 0xfeedfacf, cpu, 0, 6, ncmd, sizeof, 0, 0)
    text = struct.pack('<II16sQQQQiiII', 0x19, 72, b'__TEXT', 0, end, 0, end, 7, 5, 0, 0)
    commands = text
    if signature:
        commands += struct.pack('<II16sQQQQiiII', 0x19, 72, b'__LINKEDIT', end, len(signature), end, len(signature), 7, 1, 0, 0)
        commands += struct.pack('<IIII', 0x1d, 16, end, len(signature))
    return header + commands + payload + signature


def fat(*slices):
    header = struct.pack('>II', 0xcafebabe, len(slices))
    rows, body = b'', b''
    offset = 8 + 20 * len(slices)
    for data in slices:
        cpu, subtype = struct.unpack_from('<II', data, 4)
        rows += struct.pack('>IIIII', cpu, subtype, offset, len(data), 0)
        body += data
        offset += len(data)
    return header + rows + body


def test_exact_resource_mapping_and_no_acceptance():
    proof = module().prove_public_pair(b'public', b'public', 'raw')
    assert proof.complete
    assert proof.stable_ranges == ((0, 0, 6),)
    assert not proof.signed_acceptance
    assert proof.changed_regions == ()


def test_native_header_and_signature_are_never_public_intervals():
    data = thin(signature=b'opaque signature')
    proof = module().prove_public_pair(data, data, 'raw')
    assert proof.complete
    assert proof.stable_ranges == ((192, 192, 19),)
    assert all(kind in {'header', 'signature'} for _, _, kind in proof.excluded_regions)
    assert not proof.signed_acceptance


@pytest.mark.parametrize('transform', ['raw', 'postbuild', 'signed'])
def test_exact_thin_and_fat_mapping(transform):
    arm = thin()
    origin = fat(thin(cpu=0x1000007), arm)
    proof = module().prove_public_pair(origin, arm, transform)
    assert proof.complete
    assert proof.stable_ranges == ((104, 275, 19),)
    assert not proof.signed_acceptance


@pytest.mark.parametrize('change', ['append', 'tamper', 'truncate', 'signature', 'linkedit'])
def test_changed_native_bytes_are_unknown_even_with_controlled_data(change):
    origin = thin(signature=b'opaque signature')
    current = bytearray(origin)
    if change == 'append':
        current += b'x'
    elif change == 'tamper':
        current[192] ^= 1
    elif change == 'truncate':
        current = current[:-1]
    elif change == 'signature':
        current[-1] ^= 1
    else:
        current[144] ^= 1
    result = module().prove_public_pair(origin, bytes(current), 'signed', controlled_fields={'anything': True})
    assert not result.complete
    assert not result.stable_ranges
    assert not result.signed_acceptance


@pytest.mark.parametrize('bad', ['segment-overflow', 'overlap', 'duplicate-command', 'duplicate-slice', 'slice-overflow', 'suffix', 'nonzero-padding', 'unknown-command', 'section-overflow'])
def test_invalid_geometry_never_grants_even_identical_bytes(bad):
    data = bytearray(thin())
    if bad == 'segment-overflow':
        struct.pack_into('<Q', data, 80, 99999)
    elif bad == 'overlap':
        data = bytearray(thin(signature=b'sig'))
        struct.pack_into('<Q', data, 144, 1)
    elif bad == 'duplicate-command':
        data = bytearray(thin(signature=b'sig'))
        struct.pack_into('<I', data, 104, 0x19)
        data[112:128] = b'__TEXT'.ljust(16, b'\0')
    elif bad == 'duplicate-slice':
        data = bytearray(fat(thin(), thin()))
    elif bad == 'slice-overflow':
        data = bytearray(fat(thin()))
        struct.pack_into('>I', data, 20, 99999)
    elif bad == 'suffix':
        data += b'\0'
    elif bad == 'nonzero-padding':
        data = bytearray(fat(thin()))
        struct.pack_into('>I', data, 16, 29)
        data[28:28] = b'x'
    elif bad == 'unknown-command':
        struct.pack_into('<I', data, 32, 0x777)
    else:
        struct.pack_into('<I', data, 96, 1)
    proof = module().prove_public_pair(bytes(data), bytes(data), 'raw')
    assert not proof.complete
    assert not proof.stable_ranges


def test_nonzero_discarded_fat_gap_fails_thinning():
    arm = thin()
    data = bytearray(fat(arm))
    struct.pack_into('>I', data, 16, 29)
    data[28:28] = b'x'
    assert not module().prove_public_pair(bytes(data), arm, 'raw').complete


@pytest.mark.parametrize('transform', ['strip', '', 'RAW'])
def test_unknown_transform_fails(transform):
    assert not module().prove_public_pair(b'a', b'a', transform).complete


def test_pair_deadline_and_size_limit_fail_closed(monkeypatch):
    m = module()
    monkeypatch.setattr(m, 'MAX_PAIR_BYTES', 2)
    assert not m.prove_public_pair(b'abc', b'abc', 'raw').complete
    monkeypatch.setattr(m, 'PAIR_SECONDS', 0)
    assert not m.prove_public_pair(b'a', b'a', 'raw').complete


def signing_pair(*, replacement=False):
    """Synthetic LINKEDIT grows only by an opaque signature, with zero header slack."""
    def image(signature):
        header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, 2 + bool(signature), 144 + (16 if signature else 0), 0, 0)
        text = struct.pack('<II16sQQQQiiII', 0x19, 72, b'__TEXT', 0, 256, 0, 256, 7, 5, 0, 0)
        link = struct.pack('<II16sQQQQiiII', 0x19, 72, b'__LINKEDIT', 256, 32 + len(signature), 256, 32 + len(signature), 7, 1, 0, 0)
        command = struct.pack('<IIII', 0x1d, 16, 288, len(signature)) if signature else b''
        data = header + text + link + command
        return data.ljust(224, b'\0') + b'A' * 32 + b'B' * 32 + signature
    return image(b'oldsignature' if replacement else b''), image(b'new opaque signature')


@pytest.mark.parametrize('replacement', [False, True])
def test_controlled_signature_insertion_and_replacement_only_prove_structure(replacement):
    origin, current = signing_pair(replacement=replacement)
    proof = module().prove_public_pair(origin, current, 'signed', controlled_fields={'linkedit_vmsize': 52, 'linkedit_filesize': 52})
    assert proof.complete
    assert proof.changed_regions
    assert not proof.signed_acceptance
    assert any(start <= 224 and start + length >= 288 for start, _, length in proof.stable_ranges)
    assert all(start + length <= 288 for start, _, length in proof.stable_ranges)


@pytest.mark.parametrize('tamper', ['payload', 'slack', 'unsupported-field', 'missing-control', 'wrong-control', 'header', 'signature-offset'])
def test_signing_changes_require_exact_supported_fields_and_payload(tamper):
    origin, current = signing_pair()
    fields = {'linkedit_vmsize': 52, 'linkedit_filesize': 52}
    current = bytearray(current)
    if tamper == 'payload':
        current[224] ^= 1
    elif tamper == 'slack':
        origin = bytearray(origin)
        origin[180] = 1
        origin = bytes(origin)
    elif tamper == 'unsupported-field':
        fields['arbitrary'] = 1
    elif tamper == 'missing-control':
        fields = None
    elif tamper == 'wrong-control':
        fields['linkedit_vmsize'] = 999
    elif tamper == 'header':
        current[24] ^= 1
    else:
        struct.pack_into('<I', current, 184, 280)
    assert not module().prove_public_pair(origin, bytes(current), 'signed', controlled_fields=fields).complete


def test_zero_file_extent_is_unknown_without_parser_exception():
    data = bytearray(thin())
    struct.pack_into('<Q', data, 80, 0)
    assert not module().prove_public_pair(bytes(data), bytes(data), 'raw').complete


def test_origin_and_current_intervals_account_for_every_byte():
    origin, current = signing_pair(replacement=True)
    proof = module().prove_public_pair(origin, current, 'signed', controlled_fields={'linkedit_vmsize': 52, 'linkedit_filesize': 52})
    assert proof.complete
    current_cover = [0] * len(current)
    origin_cover = [0] * len(origin)
    for start, oldstart, size in proof.stable_ranges:
        for index in range(size):
            current_cover[start + index] += 1
            origin_cover[oldstart + index] += 1
    for start, size, _ in proof.excluded_regions:
        for index in range(start, start + size):
            current_cover[index] += 1
    for start, size, _ in proof.discarded_regions:
        for index in range(start, start + size):
            origin_cover[index] += 1
    assert set(current_cover) == {1}
    assert set(origin_cover) == {1}


def test_controlled_final_linkedit_vm_extent_cannot_overflow_uint64():
    origin, current = signing_pair()
    current = bytearray(current)
    struct.pack_into('<Q', current, 136, (1 << 64) - 1)
    proof = module().prove_public_pair(
        origin, bytes(current), 'signed',
        controlled_fields={'linkedit_vmsize': (1 << 64) - 1, 'linkedit_filesize': 52},
    )
    assert not proof.complete
    assert proof.stable_ranges == ()
    assert not proof.signed_acceptance


def _high_vm_image(*, overflowing_segment=False, overflowing_section=False):
    payload = b'public bytes....'
    vmaddr = (1 << 64) - 1024
    vmsize = 2048 if overflowing_segment or overflowing_section else 1024
    addr = (1 << 64) - 8 if overflowing_section else vmaddr + 184
    header = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, 1, 152, 0, 0)
    segment = struct.pack(
        '<II16sQQQQiiII', 0x19, 152, b'__TEXT',
        vmaddr, vmsize, 0, 200, 7, 5, 1, 0,
    )
    section = struct.pack(
        '<16s16sQQ8I', b'__text', b'__TEXT', addr, 16,
        184, 0, 0, 0, 0, 0, 0, 0,
    )
    return header + segment + section + payload


@pytest.mark.parametrize('overflow', ['segment', 'section'])
def test_segment_and_section_virtual_ranges_must_fit_uint64(overflow):
    data = _high_vm_image(
        overflowing_segment=overflow == 'segment',
        overflowing_section=overflow == 'section',
    )
    proof = module().prove_public_pair(data, data, 'raw')
    assert not proof.complete
    assert proof.stable_ranges == ()


def test_valid_high_virtual_range_preserves_exact_public_bytes():
    data = _high_vm_image()
    proof = module().prove_public_pair(data, data, 'raw')
    assert proof.complete
    assert proof.stable_ranges == ((184, 184, 16),)
    assert not proof.signed_acceptance
