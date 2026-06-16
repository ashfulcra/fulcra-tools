"""Interop guard between the coord checkpoint bridge and fulcra-continuity.

``fulcra_coord.continuity`` deliberately reimplements the continuity checkpoint
shape and stays stdlib-only — it never imports the ``fulcra-continuity``
package, so coord works on a bus where continuity isn't installed. That
independence is also how the two can silently DRIFT: PR #226's non-dict-identity
guard had to be applied to the bridge separately because the standalone already
had its own guard, and a future edit to one default/field could diverge unseen.

This test pins the interop contract WHEN both packages are present (dev/CI):
the shared bootstrap primer and the produced checkpoint key set must match. It
skips cleanly when fulcra-continuity isn't installed, so it never adds a runtime
dependency to fulcra-coord.
"""

from __future__ import annotations

import unittest

import pytest


class CoordContinuityInteropTest(unittest.TestCase):
    def test_bridge_and_standalone_checkpoint_shapes_match(self) -> None:
        standalone = pytest.importorskip("fulcra_continuity.checkpoint")
        from fulcra_coord import continuity as bridge

        self.assertEqual(
            bridge.DEFAULT_BOOTSTRAP_PRIMER,
            standalone.DEFAULT_BOOTSTRAP_PRIMER,
            "bootstrap primer drifted between the coord bridge and "
            "fulcra-continuity — a checkpoint written by one would carry a "
            "different default than the other.",
        )

        task = {
            "id": "T",
            "title": "t",
            "current_summary": "o",
            "workstream": "w",
            "owner_agent": "a",
            "status": "active",
            "next_action": "n",
        }
        bridge_keys = set(bridge.make_checkpoint(task, agent="a").keys())
        standalone_keys = set(
            standalone.make_checkpoint(
                task_id="T", title="t", objective="o",
                workstream_id="w", agent_id="a",
            ).to_dict().keys()
        )
        self.assertEqual(
            bridge_keys, standalone_keys,
            "checkpoint key set drifted between the coord bridge and "
            "fulcra-continuity — `fulcra-continuity resume` reads a different "
            "shape than the bridge writes.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
