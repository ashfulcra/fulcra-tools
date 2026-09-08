"""Remove build-only entry points and reject local paths before app signing."""
from pathlib import Path
import argparse
import shutil


def sanitize(app: Path, *, home: Path | None = None, check_only: bool = False) -> None:
    packages = app / 'Contents' / 'Resources' / 'app_packages'
    if (not packages.is_dir() or any(p.is_symlink() for p in
            (app, app / 'Contents', app / 'Contents/Resources', packages))):
        raise RuntimeError('Expected a built application with bundled packages')
    scripts = packages / 'bin'
    if scripts.is_symlink():
        raise RuntimeError('Refusing a symlinked package script directory')
    if scripts.exists():
        # Pip embeds its build-interpreter path in these shebangs. They are
        # unusable after installation; signed native launchers live in MacOS/.
        if check_only:
            raise RuntimeError('Nonportable package scripts remain in bundle')
        shutil.rmtree(scripts)
    # Reject this builder's home in all dependencies and binary metadata.
    # Generic /home/ strings also occur in public URLs and upstream SDKs;
    # the tracked-source privacy guard reviews other home-directory names.
    prefixes = (str(home or Path.home()),)
    markers = [value.encode(encoding) for value in prefixes
               for encoding in ('utf-8', 'utf-16-le', 'utf-16-be')]
    findings = 0
    for path in app.rglob('*'):
        values = [str(path.relative_to(app)).encode()]
        if path.is_symlink():
            values.append(str(path.readlink()).encode())
        elif path.is_file():
            values.append(path.read_bytes())
        if any(marker in value for value in values for marker in markers):
            findings += 1
    if findings:
        raise RuntimeError(f'Personal home path remains in {findings} bundle entries; paths are redacted')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    sanitize(args.app, check_only=args.check_only)
