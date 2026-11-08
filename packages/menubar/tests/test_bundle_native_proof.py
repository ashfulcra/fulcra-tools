"""Pure public byte mapping: no native tools, credentials, or acceptance."""
import importlib.util
import struct
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))


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


def canonical_linker_alias():
    from types import SimpleNamespace
    base='Contents/Frameworks/Python.framework'
    return SimpleNamespace(container=base,executable=base+'/Versions/3.13/Python',
        info_path=base+'/Versions/3.13/Resources/Info.plist',
        info_sha256='f4381e1bc9ed0134a026120fd580ce0d516505bb9fd046252561038244560af8',
        links=((base+'/Versions/Current','3.13'),(base+'/Python','Versions/Current/Python'),(base+'/Resources','Versions/Current/Resources')))


def test_required_linker_record_is_selected_from_owned_expected_alias():
    n=module();assert hasattr(n,'requirements_from_expected_aliases'),'required source-data record missing'
    alias=canonical_linker_alias();records=n.requirements_from_expected_aliases((alias,))
    assert len(records)==1;r=records[0]
    assert (r.path,r.bytes,r.mode,r.sha256)==('Contents/Frameworks/Python.framework/Versions/3.13/lib/python3.13/config-3.13-darwin/python.o',9440,0o644,'1243d6c664a706a1428c09c7d7db95bc77d34c77ff965eb262b46fa84e58a8b5')
    assert (r.archive_name,r.archive_sha256,r.member)==('Python-3.13-macOS-support.b10.tar.gz','1028a51fc7f3f953cac832d9252c5d2577f2f0e7a03d23ce2f1a36b702b60248','Python.xcframework/macos-arm64_x86_64/Python.framework/Versions/3.13/lib/python3.13/config-3.13-darwin/python.o')
    for field in ('container','executable','info_path','info_sha256','links'):
        from types import SimpleNamespace
        bad=vars(alias)|{field:() if field=='links' else 'crossed'}
        assert n.requirements_from_expected_aliases((SimpleNamespace(**bad),))==()
    with pytest.raises(n.LinkerDataUnknown):n.requirements_from_expected_aliases((alias,alias))


def test_inactive_policy_gains_no_linker_exemption(monkeypatch):
    n=module();assert hasattr(n,'classify_signing_input'),'pure source-data classifier missing'
    alias=canonical_linker_alias();r=n.requirements_from_expected_aliases((alias,))[0]
    assert n.classify_signing_input((),r.path,b'anything',0o755)=='ordinary'
    assert n.classify_signing_input((alias,),r.path+'.other',b'anything',0o644)=='ordinary'
    for raw,mode in ((b'wrong',0o644),(b'',0o755),(b'\x00'*9440,0o644)):
        with pytest.raises(n.LinkerDataUnknown):n.classify_signing_input((alias,),r.path,raw,mode)
    from types import SimpleNamespace
    entry=SimpleNamespace(path=r.path,kind='file',mode=r.mode,size=r.bytes,sha256=r.sha256)
    assert n.require_linker_data_inventory((alias,),(entry,))==(r,)
    for entries in ((),(entry,entry),(SimpleNamespace(**(vars(entry)|{'kind':'link'})),)):
        with pytest.raises(n.LinkerDataUnknown):n.require_linker_data_inventory((alias,),entries)


def canonical_collect_alias():
    from types import SimpleNamespace
    return SimpleNamespace(container='.',executable='Contents/MacOS/Fulcra Collect',info_path='Contents/Info.plist',info_sha256='',links=())


def test_collect_debug_data_activation_requires_both_owned_aliases():
    n=module();python=canonical_linker_alias();outer=canonical_collect_alias()
    assert len(n.requirements_from_expected_aliases((python,)))==1
    assert n.requirements_from_expected_aliases((outer,))==()
    records=n.requirements_from_expected_aliases((python,outer))
    assert len(records)==3, 'exact Collect debug companions are missing'
    expected=[('objc/_machsignals.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/_machsignals.cpython-313-darwin.so',29114,'e28126b48403e9b288ad02cedd628a55c4b57b56bc7ff31b02bfbb88ca8ec1c4'),('objc/_objc.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/_objc.cpython-313-darwin.so',3699949,'22f94e97cb8b9729df97d0ffb790ae70ae278afafa07553b4c34fbf97cf6134f')]
    for record,(member,size,sha256) in zip(records[1:],expected):
        assert record.path=='Contents/Resources/app_packages/'+member and record.member==member
        assert (record.bytes,record.mode,record.sha256)==(size,0o644,sha256)
        assert (record.archive_name,record.archive_sha256)==('pyobjc_core-12.2.2-cp313-cp313-macosx_10_13_universal2.whl','950bd2d9c74634398c4e3d24ef2f213d4e23d705083697464fa67afedc53c1ad')
        assert n.classify_signing_input((python,),record.path,b'other',0o644)=='ordinary'
        assert n.classify_signing_input((python,outer),record.path+'.extra',b'other',0o644)=='ordinary'
    for field in ('container','executable','info_path','info_sha256','links'):
        from types import SimpleNamespace
        changed=SimpleNamespace(**(vars(outer)|{field:(('wrong','wrong'),) if field=='links' else 'wrong'}))
        assert len(n.requirements_from_expected_aliases((python,changed)))==1


def test_debug_origin_records_are_complete_and_fixed():
    n=module();origins=n._DEBUG_DATA_ORIGINS
    assert tuple((o.archive_bytes,o.member_bytes,o.member_sha256,o.slice_offset) for o in origins)==(
        (6427113,58202,'66c0fc7c1f2c3372ec613761cf6a4482a33a9e8a4f09b312c9ed31a11fa4280b',29088),
        (6427113,7267629,'4b868a052995011fa4a234b8615b8aded27327bd9bd4d02af73805a52a8ad924',3567680))
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):origins[0].slice_offset=0
    with pytest.raises(n.LinkerDataUnknown):n.requirements_from_expected_aliases((canonical_linker_alias(),canonical_collect_alias(),canonical_collect_alias()))
