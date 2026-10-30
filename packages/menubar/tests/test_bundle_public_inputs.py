"""Synthetic archives exercise exact origin and complete scanner failure modes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import sys
import zipfile

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'


def module():
    name = 'bundle_provenance'
    if name not in sys.modules:
        sys.path.insert(0, str(SCRIPTS))
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
        value = importlib.util.module_from_spec(spec)
        sys.modules[name] = value
        spec.loader.exec_module(value)
    return sys.modules[name]


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    m = module()
    archive = tmp_path / 'example-1-cp313-cp313-macosx_11_0_arm64.whl'
    destination = 'Contents/Resources/app_packages/example/data.bin'
    payload = b'prefix /public/marker suffix'
    member = 'example/data.bin'
    lock = tmp_path / 'uv.lock'
    catalog = tmp_path / 'catalog.json'

    def make(*, names=None, info=None, data=payload, member_name=member):
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            if info is not None:
                z.writestr(info, data)
            else:
                z.writestr(member_name, data)
            for name in names or []:
                z.writestr(name, b'extra')
        raw = archive.read_bytes()
        url = 'https://example.invalid/' + archive.name
        lock.write_text(f'[[package]]\nname = "example"\nversion = "1"\nwheels = [{{url = "{url}", hash = "sha256:{sha(raw)}", size = {len(raw)}}}]\n')
        value = {'schema': 1, 'architecture': 'arm64', 'archives': [{'name': archive.name, 'kind': 'wheel', 'package': 'example', 'version': '1', 'tag': 'cp313-cp313-macosx_11_0_arm64', 'url': url, 'bytes': len(raw), 'sha256': sha(raw)}], 'members': [{'destination': destination, 'archive': archive.name, 'member': member, 'bytes': len(data), 'sha256': sha(data), 'forms': ['exact']}]}
        catalog.write_text(json.dumps(value))
        return value

    make()
    monkeypatch.setattr(m, '_CATALOG_PATH', catalog)

    def load(**kwargs):
        return m.load_public_inputs(kwargs.pop('archives', {archive.name: archive}), lock=kwargs.pop('lock', lock), architecture=kwargs.pop('architecture', 'arm64'), expected_catalog_sha256=kwargs.pop('expected_catalog_sha256', sha(catalog.read_bytes())), **kwargs)

    return m, archive, catalog, lock, destination, payload, make, load


def test_exact_public_member_is_bound_to_catalog_destination(inputs, tmp_path):
    m, _, _, _, destination, payload, _, load = inputs
    verified = load()
    assert verified.complete
    assert len(verified.members) == 1
    assert payload.decode() not in repr(verified)
    app = tmp_path / 'app'
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    result = m.scan_bundle(app, ('/public/marker',), public_inputs=verified)
    assert result.status == 'COMPLETE'
    assert result.privacy_rejections == 0
    assert result.permitted_hits == 1
    assert not result.signed_acceptance
    assert not result.application_acceptance


@pytest.mark.parametrize('failure', ['archive-hash', 'catalog-hash', 'missing', 'extra', 'arch', 'lock', 'member', 'destination', 'member-hash', 'version', 'tag'])
def test_unbound_input_is_unknown_even_without_marker(inputs, tmp_path, failure):
    m, archive, catalog, lock, destination, payload, make, load = inputs
    args = {}
    if failure == 'archive-hash':
        archive.write_bytes(archive.read_bytes() + b'changed')
    elif failure == 'catalog-hash':
        args['expected_catalog_sha256'] = '0' * 64
    elif failure == 'missing':
        args['archives'] = {}
    elif failure == 'extra':
        args['archives'] = {archive.name: archive, 'other': archive}
    elif failure == 'arch':
        args['architecture'] = 'x86_64'
    elif failure == 'lock':
        lock.write_text('[[package]]\nname="example"\nversion="1"\nwheels=[]\n')
    else:
        value = json.loads(catalog.read_text())
        if failure == 'member':
            value['members'][0]['member'] = 'wrong'
        elif failure == 'destination':
            value['members'][0]['destination'] = '../outside'
        elif failure == 'member-hash':
            value['members'][0]['sha256'] = '0' * 64
        else:
            value['archives'][0][failure] = 'wrong'
        catalog.write_text(json.dumps(value))
    verified = load(**args)
    assert not verified.complete
    app = tmp_path / 'app'
    app.mkdir()
    result = m.scan_bundle(app, (), public_inputs=verified)
    assert result.status == 'UNKNOWN'
    assert not result.application_acceptance


@pytest.mark.parametrize('name', ['example/data.bin', 'EXAMPLE/DATA.BIN', '../escape', '/absolute', 'example/../escape', 'example\\escape'])
def test_all_zip_paths_reject_duplicate_collision_or_traversal(inputs, name):
    _, _, _, _, _, _, make, load = inputs
    with pytest.warns(UserWarning) if name == 'example/data.bin' else __import__('contextlib').nullcontext():
        make(names=[name])
    assert not load().complete


def test_zip_symlink_is_rejected(inputs):
    _, _, _, _, _, _, make, load = inputs
    info = zipfile.ZipInfo('example/data.bin')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    make(info=info)
    assert not load().complete


@pytest.mark.parametrize('limit', ['MAX_ARCHIVE_BYTES', 'MAX_ARCHIVE_TOTAL', 'MAX_ZIP_ENTRIES', 'MAX_MEMBER_BYTES', 'MAX_MEMBER_TOTAL', 'MAX_CATALOG_BYTES', 'MAX_CATALOG_RECORDS'])
def test_input_caps_fail_closed(inputs, monkeypatch, limit):
    m, _, _, _, _, _, _, load = inputs
    monkeypatch.setattr(m, limit, 0)
    assert not load().complete


def test_corrupt_and_encrypted_archives_fail_after_identity_check(inputs):
    _, archive, catalog, lock, _, _, _, load = inputs
    for encrypted in [False, True]:
        data = bytearray(archive.read_bytes())
        if encrypted:
            start = data.find(b'PK\x01\x02')
            struct.pack_into('<H', data, start + 8, 1)
        else:
            data[40] ^= 0xff
        archive.write_bytes(data)
        value = json.loads(catalog.read_text())
        value['archives'][0].update(bytes=len(data), sha256=sha(data))
        catalog.write_text(json.dumps(value))
        lock.write_text(f'[[package]]\nname="example"\nversion="1"\nwheels=[{{url="{value["archives"][0]["url"]}",hash="sha256:{sha(data)}",size={len(data)}}}]\n')
        assert not load().complete


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16-le', 'utf-16-be'])
def test_scanner_finds_encoded_overlapping_hits(tmp_path, encoding):
    m = module()
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'data').write_bytes('ababa'.encode(encoding))
    result = m.scan_bundle(app, ('aba',))
    assert result.status == 'COMPLETE'
    # UTF-16 also contains one opposite-endian match shifted by one byte.
    assert result.privacy_rejections == (2 if encoding == 'utf-8' else 3)


@pytest.mark.parametrize('mutation', ['moved', 'altered', 'name', 'link', 'ancestor', 'special'])
def test_public_bytes_do_not_exempt_other_surfaces(inputs, tmp_path, mutation):
    m, _, _, _, destination, payload, _, load = inputs
    verified = load()
    app = tmp_path / 'app'
    target = app / destination
    target.parent.mkdir(parents=True)
    if mutation == 'moved':
        (app / 'other').write_bytes(payload)
    elif mutation == 'altered':
        target.write_bytes(payload + b'extra')
    elif mutation == 'name':
        (app / 'public-marker').write_bytes(b'clean')
    elif mutation == 'link':
        (app / 'link').symlink_to('/public/marker')
    elif mutation == 'ancestor':
        other = tmp_path / 'other'
        other.mkdir()
        (other / 'data').write_bytes(payload)
        (app / 'linked').symlink_to(other, target_is_directory=True)
    else:
        os.mkfifo(app / 'fifo')
    result = m.scan_bundle(app, ('/public/marker', 'public-marker'), public_inputs=verified)
    if mutation in {'ancestor', 'special'}:
        assert result.status == 'UNKNOWN'
    else:
        assert result.privacy_rejections >= 1
    assert not result.application_acceptance


@pytest.mark.parametrize('phase', ['postbuild', 'signed', 'unknown'])
def test_stage_name_cannot_grant_transformed_authority(inputs, tmp_path, phase):
    m, _, _, _, _, _, _, load = inputs
    app = tmp_path / 'app'
    app.mkdir()
    assert m.scan_bundle(app, (), public_inputs=load(), phase=phase).status == 'UNKNOWN'


@pytest.mark.parametrize('limit', ['MAX_APP_ENTRIES', 'MAX_FILE_BYTES', 'MAX_TOTAL_BYTES', 'MAX_DEPTH', 'MAX_PATH_BYTES', 'SCAN_SECONDS'])
def test_scan_caps_never_report_clean_empty(tmp_path, monkeypatch, limit):
    m = module()
    app = tmp_path / 'app'
    (app / 'nested').mkdir(parents=True)
    (app / 'nested/data').write_bytes(b'clean')
    monkeypatch.setattr(m, limit, 0)
    result = m.scan_bundle(app, ())
    assert result.status == 'UNKNOWN'


def test_framework_links_are_inventoried_without_following(tmp_path):
    m = module()
    app = tmp_path / 'app'
    (app / 'Versions/A').mkdir(parents=True)
    (app / 'Versions/A/data').write_bytes(b'clean')
    (app / 'Versions/Current').symlink_to('A')
    result = m.scan_bundle(app, ())
    assert result.status == 'COMPLETE'
    assert result.symlinks == 1
    assert result.files == 1


def test_changing_file_and_enumeration_failure_are_unknown(tmp_path, monkeypatch):
    m = module()
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'data').write_bytes(b'clean')
    original = m.os.read

    def changing(fd, size):
        data = original(fd, size)
        (app / 'data').write_bytes(b'changed')
        return data

    with monkeypatch.context() as patch:
        patch.setattr(m.os, 'read', changing)
        assert m.scan_bundle(app, ()).status == 'UNKNOWN'
    def broken(*args):
        raise OSError('private path must not escape')
    monkeypatch.setattr(m.os, 'scandir', broken)
    result = m.scan_bundle(app, ())
    assert result.status == 'UNKNOWN'
    assert 'private path' not in repr(result)


def test_symlink_archive_and_root_ancestors_fail(inputs, tmp_path):
    m, archive, _, _, _, _, _, load = inputs
    linked = tmp_path / 'linked'
    linked.symlink_to(tmp_path, target_is_directory=True)
    assert not load(archives={archive.name: linked / archive.name}).complete
    app = tmp_path / 'app'
    app.mkdir()
    assert m.scan_bundle(linked / 'app', ()).status == 'UNKNOWN'


def test_marker_crossing_native_header_and_public_payload_rejects(inputs, tmp_path):
    m, _, _, _, destination, _, make, load = inputs
    payload = b'public-marker'
    end = 104 + len(payload)
    data = struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 6, 1, 72, 0, 0)
    data += struct.pack('<II16sQQQQiiII', 0x19, 72, b'__TEXT', 0, end, 0, end, 7, 5, 0, 0) + payload
    make(data=data)
    app = tmp_path / 'app'
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    result = m.scan_bundle(app, ('\0public',), public_inputs=load())
    assert result.status == 'COMPLETE'
    assert result.privacy_rejections == 1
    assert result.permitted_hits == 0


def test_missing_bound_destination_and_read_failure_cannot_be_empty_success(inputs, tmp_path, monkeypatch):
    m, _, _, _, destination, payload, _, load = inputs
    verified = load()
    app = tmp_path / 'app'
    app.mkdir()
    assert m.scan_bundle(app, (), public_inputs=verified).status == 'UNKNOWN'
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    def fail(*args):
        raise OSError('sensitive error')
    monkeypatch.setattr(m, '_read_at', fail)
    result = m.scan_bundle(app, (), public_inputs=verified)
    assert result.status == 'UNKNOWN'
    assert 'sensitive' not in repr(result)


def test_scanner_deadline_covers_overlapping_match_iteration(tmp_path, monkeypatch):
    m = module()
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'data').write_bytes(b'a' * 100)
    clock = iter([0.0] * 30 + [1000.0] * 1000)
    monkeypatch.setattr(m.time, 'monotonic', lambda: next(clock))
    result = m.scan_bundle(app, ('a',))
    assert result.status == 'UNKNOWN'
    assert result.privacy_rejections < 100


def test_local_header_encryption_flag_cannot_hide_behind_central_directory(inputs):
    _, archive, catalog, lock, _, _, _, load = inputs
    data = bytearray(archive.read_bytes())
    struct.pack_into('<H', data, 6, 1)
    archive.write_bytes(data)
    value = json.loads(catalog.read_text())
    value['archives'][0]['sha256'] = sha(data)
    catalog.write_text(json.dumps(value))
    lock.write_text(f'[[package]]\nname="example"\nversion="1"\nwheels=[{{url="{value["archives"][0]["url"]}",hash="sha256:{sha(data)}",size={len(data)}}}]\n')
    assert not load().complete


@pytest.mark.parametrize('encoding', ['utf-16-le', 'utf-16-be'])
def test_exact_public_utf16_bytes_receive_only_contiguous_origin_permission(inputs, tmp_path, encoding):
    m, _, _, _, destination, _, make, load = inputs
    payload = 'prefix /public/marker suffix'.encode(encoding)
    make(data=payload)
    app = tmp_path / 'app'
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    result = m.scan_bundle(app, ('/public/marker',), public_inputs=load())
    assert result.status == 'COMPLETE'
    assert result.privacy_rejections == 0
    assert result.permitted_hits >= 1


def test_unsupported_native_origin_never_grants_public_marker_permission(inputs, tmp_path):
    m, _, _, _, destination, _, make, load = inputs
    payload = b'\xfe\xed\xfa\xcf' + b'/public/marker'
    make(data=payload)
    app = tmp_path / 'app'
    target = app / destination
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    result = m.scan_bundle(app, ('/public/marker',), public_inputs=load())
    assert result.privacy_rejections >= 1
    assert result.permitted_hits == 0


def test_mutation_of_earlier_file_during_later_read_is_unknown(tmp_path, monkeypatch):
    m = module()
    app = tmp_path / 'app'
    app.mkdir()
    for name in ('a', 'b'):
        (app / name).write_bytes(b'clean')
    original = m._read_at
    seen = []
    def mutate(fd, name, limit, deadline):
        data = original(fd, name, limit, deadline)
        seen.append(name)
        if len(seen) == 2:
            (app / seen[0]).write_bytes(b'late /public/marker')
        return data
    monkeypatch.setattr(m, '_read_at', mutate)
    assert m.scan_bundle(app, ('/public/marker',)).status == 'UNKNOWN'


def test_underdeclared_decompression_output_is_not_a_verified_empty_member(inputs):
    _, archive, catalog, lock, _, _, make, load = inputs
    make(data=b'A' * 10000)
    data = bytearray(archive.read_bytes())
    central = data.find(b'PK\x01\x02')
    # Dishonest classic ZIP metadata claims no output/CRC although the deflate
    # stream produces 10000 bytes; ZipExtFile alone truncates to the declaration.
    struct.pack_into('<I', data, 14, 0)
    struct.pack_into('<I', data, 22, 0)
    struct.pack_into('<I', data, central + 16, 0)
    struct.pack_into('<I', data, central + 24, 0)
    archive.write_bytes(data)
    value = json.loads(catalog.read_text())
    value['archives'][0]['sha256'] = sha(data)
    value['members'][0].update(bytes=0, sha256=sha(b''))
    catalog.write_text(json.dumps(value))
    lock.write_text(f'[[package]]\nname="example"\nversion="1"\nwheels=[{{url="{value["archives"][0]["url"]}",hash="sha256:{sha(data)}",size={len(data)}}}]\n')
    assert not load().complete


def test_zip_entry_bound_precedes_index_construction(inputs, monkeypatch):
    m, _, _, _, _, _, make, load = inputs
    make(names=['extra'])
    monkeypatch.setattr(m, 'MAX_ZIP_ENTRIES', 1)
    def expensive_constructor(*args, **kwargs):
        raise AssertionError('central index must not be built past the entry bound')
    monkeypatch.setattr(m.zipfile, 'ZipFile', expensive_constructor)
    assert not load().complete


def test_parent_component_cannot_normalize_away_a_symlink_input_ancestor(inputs, tmp_path):
    _, archive, _, _, _, _, _, load = inputs
    link = tmp_path / 'linked'
    link.symlink_to(tmp_path, target_is_directory=True)
    assert not load(archives={archive.name: link / '..' / archive.name}).complete


def test_complete_relative_path_marker_split_across_directories_rejects(tmp_path):
    m = module()
    app = tmp_path / 'app'
    target = app / 'Contents/Resources/Users/review-person/empty'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'')
    result = m.scan_bundle(app, ('/Users/review-person',))
    assert result.status == 'COMPLETE'
    assert result.privacy_rejections >= 1
    assert not result.application_acceptance


def test_absolute_app_parent_is_not_a_packaged_path_marker(tmp_path):
    m = module()
    app = tmp_path / 'Users/review-person/app'
    target = app / 'Contents/Resources/empty'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'')
    result = m.scan_bundle(app, ('/Users/review-person',))
    assert result.status == 'COMPLETE'
    assert result.privacy_rejections == 0


def _repin_synthetic_archive(archive, catalog, lock, data):
    """Preserve trusted synthetic identities so a test reaches ZIP validation."""
    archive.write_bytes(data)
    value = json.loads(catalog.read_text())
    record = value['archives'][0]
    record.update(bytes=len(data), sha256=sha(data))
    catalog.write_text(json.dumps(value))
    lock.write_text(
        f'[[package]]\nname="example"\nversion="1"\n'
        f'wheels=[{{url="{record["url"]}",hash="sha256:{sha(data)}",size={len(data)}}}]\n'
    )


@pytest.mark.parametrize('field', ['crc', 'compressed-size', 'uncompressed-size', 'all'])
def test_local_zip_crc_and_sizes_must_match_central_without_descriptor(inputs, field):
    _, archive, catalog, lock, _, _, _, load = inputs
    data = bytearray(archive.read_bytes())
    assert struct.unpack_from('<H', data, 6)[0] & 8 == 0
    offsets = {'crc': 14, 'compressed-size': 18, 'uncompressed-size': 22}
    for offset in offsets.values() if field == 'all' else [offsets[field]]:
        assert struct.unpack_from('<I', data, offset)[0] != 0
        struct.pack_into('<I', data, offset, 0)
    _repin_synthetic_archive(archive, catalog, lock, data)
    assert not load().complete


def test_unselected_zip_entry_local_sizes_must_also_match_central(inputs):
    _, archive, catalog, lock, _, _, make, load = inputs
    make(names=['extra'])
    data = bytearray(archive.read_bytes())
    offset = data.find(b'PK\x03\x04', 4)
    assert offset > 0 and struct.unpack_from('<H', data, offset + 6)[0] & 8 == 0
    struct.pack_into('<III', data, offset + 14, 0, 0, 0)
    _repin_synthetic_archive(archive, catalog, lock, data)
    assert not load().complete


def test_zip_data_descriptor_form_is_explicitly_unsupported(inputs):
    import io

    _, archive, catalog, lock, _, payload, _, load = inputs

    class NonSeekable(io.BytesIO):
        def seek(self, *args):
            raise io.UnsupportedOperation('nonseekable synthetic ZIP writer')

    stream = NonSeekable()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('example/data.bin', payload)
    data = stream.getvalue()
    assert struct.unpack_from('<H', data, 6)[0] & 8
    assert b'PK\x07\x08' in data
    _repin_synthetic_archive(archive, catalog, lock, data)
    assert not load().complete
