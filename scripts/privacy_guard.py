#!/usr/bin/env python3
"""Scan the public tracked tree without echoing matched personal values.

This checks high-confidence patterns, not every possible form of personal data.
Names, measurements, and fixture provenance still require independent review.
Run Gitleaks on --export-to output for credential detection.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

PLACEHOLDER_USERS = {'example', 'user', 'tester', 'test', 'runner', 'shared',
                     'alice', 'bob', 'me', 'myuser', 'developer', 'username',
                     'redacted', 'linuxbrew'}
HOME = re.compile(r'(?<![\w/])(?:file://)?/(?:Users|home)/([A-Za-z][A-Za-z0-9_.-]+)(?=/|\b)')
EMAIL = re.compile(r'(?<![\w.+-])[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})(?![\w.-])')
PUBLIC_EMAILS = {'support@fulcradynamics.com'}
EXAMPLE_DOMAINS = {'example.com', 'example.org', 'example.net', 'localhost.localdomain'}
PRIVATE_CONFIG_NAMES = {'.env', 'linear.env', 'answers-linear-ids.json'}
LIMIT = 64 * 1024 * 1024


def redact_location(name: str) -> str:
    name = EMAIL.sub('<email>', name)
    return HOME.sub('<home>', name)


def scan_text(name: str, text: str) -> list[tuple[str, int, str]]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(m.group(1).lower() not in PLACEHOLDER_USERS for m in HOME.finditer(line)):
            findings.append((name, number, 'personal home-directory path'))
        for match in EMAIL.finditer(line):
            address, domain = match.group().lower(), match.group(1).lower()
            if re.fullmatch(r'[123]x\.(png|jpg|jpeg|webp)', domain):
                continue
            if (address in PUBLIC_EMAILS or domain in EXAMPLE_DOMAINS
                    or any(domain.endswith('.' + item) for item in EXAMPLE_DOMAINS)
                    or domain.endswith(('.test', '.invalid', '.example'))):
                continue
            findings.append((name, number, 'non-example email address'))
    return findings


def scan_blob(name: str, data: bytes, *, depth: int = 0) -> list[tuple[str, int, str]]:
    findings = scan_text(name, name)
    member = name.rsplit('::', 1)[-1].replace('\\', '/')
    if member.rsplit('/', 1)[-1] in PRIVATE_CONFIG_NAMES:
        findings.append((name, 0, 'private account configuration file'))
    if len(data) > LIMIT:
        return [(name, 0, 'file exceeds privacy scan size limit')]
    if name.endswith(('.tar.gz', '.tgz', '.tar', '.zip')):
        if depth >= 3:
            return [(name, 0, 'archive nesting exceeds privacy scan limit')]
        total = 0
        try:
            if name.endswith('.zip'):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    findings += scan_text(name, archive.comment.decode(errors='replace'))
                    for item in archive.infolist():
                        location = name + '::' + item.filename
                        findings += scan_text(location, item.filename)
                        findings += scan_text(location, (item.comment + item.extra).decode(errors='replace'))
                        total += item.file_size
                        if total > LIMIT:
                            raise ValueError('archive too large')
                        if not item.is_dir():
                            findings += scan_blob(name + '::' + item.filename,
                                                  archive.read(item), depth=depth + 1)
            else:
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    for item in archive:
                        location = name + '::' + item.name
                        findings += scan_text(location, item.name + '\n' + item.linkname)
                        if item.uname or item.gname or item.uid or item.gid:
                            findings.append((location, 0, 'archive owner metadata must be empty'))
                        findings += scan_text(location, str(item.pax_headers))
                        total += item.size
                        if total > LIMIT:
                            raise ValueError('archive too large')
                        if item.isfile():
                            findings += scan_blob(name + '::' + item.name,
                                                  archive.extractfile(item).read(), depth=depth + 1)
        except (ValueError, OSError, RuntimeError, NotImplementedError,
                tarfile.TarError, zipfile.BadZipFile):
            findings.append((name, 0, 'archive could not be fully scanned'))
        return findings
    findings += scan_text(name, data.decode('utf-8', errors='replace'))
    return findings


def tracked_tree(root: Path):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
    for name in names:
        if not name:
            continue
        path = root / name
        if path.is_symlink():
            yield name, None
        elif path.is_file():
            yield name, path.read_bytes()


def self_test() -> None:
    bad = b'contact: private-person@' + b'personal-domain.com\npath: /Users/' + b'private-person/work\n'
    hits = scan_blob('example.txt', bad)
    assert {x[2] for x in hits} == {'non-example email address', 'personal home-directory path'}
    assert not scan_blob('example.txt', b'user@example.com /Users/example/work')
    assert not scan_blob('example.txt', b'user@shop.example.com icon@2x.png https://example.com/root/home/page')
    assert scan_blob('example.txt', b'file:///Users/' + b'private-person/work')
    for filename in ('answers-linear-ids.json', 'linear.env', '.env'):
        assert any(hit[2] == 'private account configuration file'
                   for hit in scan_blob('tools/example/' + filename, b'{}'))
    assert not scan_blob('answers-linear-ids.example.json', b'{}')
    private_archive = io.BytesIO()
    with zipfile.ZipFile(private_archive, 'w') as archive:
        archive.writestr('answers-linear-ids.json', b'{}')
    assert any(hit[2] == 'private account configuration file'
               for hit in scan_blob('fixture.zip', private_archive.getvalue()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('nested.txt', bad)
    assert len(scan_blob('fixture.zip', buffer.getvalue())) == 2
    encrypted = bytearray(buffer.getvalue())
    # Mark a synthetic member encrypted in both ZIP headers.
    for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
        pos = encrypted.index(signature) + offset
        encrypted[pos] |= 1
    assert any(hit[2] == 'archive could not be fully scanned'
               for hit in scan_blob('encrypted.zip', bytes(encrypted)))
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode='w') as archive:
        item = tarfile.TarInfo('private-person@' + 'personal-domain.com')
        item.uname = 'private-owner'
        item.type = tarfile.SYMTYPE
        item.linkname = '/Users/' + 'private-person/file'
        archive.addfile(item)
    assert len(scan_blob('fixture.tar', archive_buffer.getvalue())) >= 3
    assert '@' not in redact_location(item.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--export-to', type=Path)
    args = parser.parse_args()
    self_test()
    if args.self_test:
        print('Privacy self-test passed: planted path, email, private config, and archive findings detected.')
        return 0
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    if args.export_to:
        args.export_to.mkdir(parents=True, exist_ok=True)
        if any(args.export_to.iterdir()):
            parser.error('--export-to must name an empty directory')
    findings = []
    count = 0
    for name, data in tracked_tree(root):
        count += 1
        if name.startswith(('.superpowers/', '.private/', '.codex/')):
            findings.append((name, 0, 'local agent material tracked publicly'))
        if data is None:
            findings.append((name, 0, 'symlink requires explicit privacy review'))
            continue
        findings += scan_blob(name, data)
        if args.export_to:
            target = args.export_to / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    for name, line, category in findings:
        print(f'{redact_location(name)}:{line}: {category}')
    print(f'Privacy scan: {count} tracked files; {len(findings)} findings. Values are redacted.')
    return int(bool(findings))


if __name__ == '__main__':
    raise SystemExit(main())
