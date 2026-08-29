"""Test-environment isolation for Linear credentials.

Real Linear credentials live in the environment of every box that runs this
suite, under several names. A test that only deletes LINEAR_API_KEY used to
be testing "no credentials" while three other working ones were still set --
so the suite's answer depended on whose machine it ran on. Clear all of them
once, and let each test set exactly what it means to test.
"""

import pytest

from coord_tracker_bridge.cli import LINEAR_KEY_ENV_VARS


@pytest.fixture(autouse=True)
def _no_ambient_linear_credentials(monkeypatch):
    for name in (*LINEAR_KEY_ENV_VARS, "LINEAR_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)
