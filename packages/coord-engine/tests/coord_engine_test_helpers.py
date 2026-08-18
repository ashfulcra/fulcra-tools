"""Package-prefixed test helpers (root-pythonpath convention — see root pyproject).

Re-exports the transport fakes defined in test_reconcile so sibling test modules
can import them without a `tests.`-package path (which breaks under the monorepo
root's importlib collection: the hyphen in `coord-engine` is not a valid package
name segment).
"""

from test_reconcile import FakeTransport, _task  # noqa: F401


def needs_me_rows(value):
    """Unwrap a needs-me machine read across the contract boundary.

    Contract 2 (OC2 ladder PR 1) made `needs-me --json` emit ONE envelope
    object with the rows inside; every other Class A verb still emits the
    contract-1 bare array until its own ladder PR. Tests that assert on ROWS
    go through this so each verb's flip only touches its own fixtures.
    """
    if isinstance(value, dict) and "rows" in value:
        return value["rows"]
    return value
