import pytest
from fulcra_netflix import loader


@pytest.fixture(scope="session")
def ni():
    """The netflix_import script, loaded as a module."""
    return loader.load()


@pytest.fixture()
def fixtures_dir(request):
    from pathlib import Path
    return Path(request.config.rootpath) / "packages/netflix-skill/tests/fixtures"
