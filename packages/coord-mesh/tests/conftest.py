"""No unit test in this package may touch the live platform.

WHY THIS FILE EXISTS. `test_send_write_failure_is_unknown_not_success` patched
`transport.record` and left the pre-write snapshot unpatched, so the snapshot
shelled out to the real `fulcra-api`. On a developer host with credentials that
read SUCCEEDED, the test proceeded to the patched write, and the assertion
passed — for a run that had just performed a live network read inside a unit
test. On CI, where the runner holds no credentials, the same read failed, the
r3 guard correctly refused before reaching the patched write, and the test went
red (coord-boss's diagnosis of job 95745650060 at 7e4aae59).

The test measured the network's cooperation, not the code path it names. That
is this package's thesis wearing test clothing, and auditing for it by eye is
the same mistake one level up — so it is enforced instead.

Every route from this package to the outside world goes through two functions,
`transport.run` and `transport.record`. Both are stubbed here to RAISE. A test
that wants transport behaviour must say so by patching them itself, which
monkeypatch lets it do on top of this fixture; a test that reaches the network
by omission gets a named failure instead of a silent live call.

The credential-less CI runner remains the independent check: it can only be
green if nothing here touches the platform. This fixture makes local runs agree
with it rather than discovering the disagreement a round later.
"""
import pytest

from coord_mesh import transport


class LiveCallInTest(AssertionError):
    """A unit test tried to reach the real platform."""


@pytest.fixture(autouse=True)
def no_live_transport(monkeypatch):
    def _refuse_run(args, **_):
        raise LiveCallInTest(
            f"unit test reached transport.run({list(args)!r}) — the real "
            "fulcra-api. Patch the transport surface this test needs "
            "(get_records / list_outgoing / list_incoming / share_create / "
            "supports_file_grants), or patch transport.run itself if the argv "
            "is what you are asserting. Half-patching is how the r7 "
            "hermeticity defect passed locally and failed on CI."
        )

    def _refuse_record(*a, **k):
        raise LiveCallInTest(
            "unit test reached transport.record — the real fulcra-api write "
            "path. Patch it explicitly."
        )

    monkeypatch.setattr(transport, "run", _refuse_run)
    monkeypatch.setattr(transport, "record", _refuse_record)
