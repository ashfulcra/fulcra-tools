"""Static release assets that Briefcase otherwise replaces without failing."""
from pathlib import Path
import tomllib


MENUBAR = Path(__file__).parents[1]


def test_briefcase_macos_icon_exists_in_native_format():
    data = tomllib.loads((MENUBAR / 'pyproject.toml').read_text())
    icon_stem = data['tool']['briefcase']['app']['fulcra-menubar']['icon']

    assert (MENUBAR / f'{icon_stem}.icns').is_file()
