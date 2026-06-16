"""Regression guard: the hermetic test fixture must neutralize the LIVE
annotation-write path, so no test can ever POST to the operator's real
Agent-Tasks timeline.

WHY THIS EXISTS — a real incident, not a nicety:

The annotation writer resolves its mode from ``FULCRA_COORD_ANNOTATIONS`` (env)
> a persisted file at ``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/annotations``
> ``off``. An operator who runs ``fulcra-coord annotations on`` persists ``on``
there machine-wide. The hermetic conftest fixture isolates
``XDG_CACHE_HOME`` and defaults the *CLI* backend to ``false`` — but the
annotation writer uses ``urllib`` directly, NOT the CLI backend, and reads its
mode from ``XDG_CONFIG_HOME`` (which the fixture did NOT isolate). So end-to-end
command tests (``cmd_start`` / ``cmd_tell`` / ``cmd_update`` …) that emit a
lifecycle annotation as a side effect, on a machine with ``http`` persisted,
resolved mode ``http``, obtained a real bearer token, and POSTed fixture titles
("Fix the widget pipeline", "do x", "t1", "resolve me", …) to the operator's
LIVE timeline. The pre-push hook runs the full suite, so every push that
touched fulcra-coord re-polluted the timeline — corrupting the exact surface
the operator's situational-awareness reports read from.

The fix: the conftest fixture force-disables annotations by default
(``FULCRA_COORD_ANNOTATIONS=off``) for every test, mirroring the existing
``FULCRA_COORD_BACKEND=false`` safety net. Tests that specifically exercise the
annotation modes set the env var themselves and still win. This file is the
guard that the safety net stays in place.
"""

from __future__ import annotations

import os
import tempfile

from fulcra_coord import annotations


def test_fixture_forces_annotations_off_by_default():
    """With no explicit per-test override, the fixture must resolve mode ``off``."""
    # The autouse hermetic fixture runs before this test and (by default) sets
    # FULCRA_COORD_ANNOTATIONS=off in the environment.
    assert os.environ.get("FULCRA_COORD_ANNOTATIONS") == "off"


def test_persisted_on_config_cannot_leak_under_fixture():
    """Even if a persisted ``on`` config file exists, the fixture's env default
    must win (env > config), so ``_mode()`` is ``off`` and the live HTTP path is
    never reached."""
    # Simulate the operator's machine-wide enablement in an isolated config root.
    cfg = tempfile.mkdtemp(prefix="fulcra-coord-test-cfg-")
    prev_cfg = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = cfg
    try:
        annotations.set_persisted_mode("on")
        assert annotations._persisted_mode() == "on"  # config really says on
        # ...yet the fixture's env default disables it:
        assert annotations._mode() == "off"
    finally:
        if prev_cfg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = prev_cfg


def test_explicit_env_override_still_wins():
    """A test that deliberately sets a mode must still override the default."""
    prev = os.environ.get("FULCRA_COORD_ANNOTATIONS")
    os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
    try:
        assert annotations._mode() == "on"
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = prev
