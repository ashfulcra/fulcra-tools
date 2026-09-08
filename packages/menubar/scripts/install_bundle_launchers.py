"""Build native, relocatable CLI entry points before Briefcase signs the app."""
from pathlib import Path
import shutil
import subprocess
import sys


def install(app: Path) -> None:
    bin_dir = app / 'Contents' / 'MacOS'
    if not bin_dir.is_dir():
        raise RuntimeError('Create the app bundle before installing CLI entry points')
    source = Path(__file__).with_name('cli_launcher.c')
    target = bin_dir / 'fulcra-collect'
    subprocess.run(['/usr/bin/clang', '-Os', '-mmacosx-version-min=12.0',
                    str(source), '-o', str(target)], check=True)
    for name in ('fulcra', 'fulcra-api'):
        shutil.copy2(target, bin_dir / name)


if __name__ == '__main__':
    install(Path(sys.argv[1]))
