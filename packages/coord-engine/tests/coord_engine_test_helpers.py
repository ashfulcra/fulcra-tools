"""Package-prefixed test helpers (root-pythonpath convention — see root pyproject).

Re-exports the transport fakes defined in test_reconcile so sibling test modules
can import them without a `tests.`-package path (which breaks under the monorepo
root's importlib collection: the hyphen in `coord-engine` is not a valid package
name segment).
"""

from test_reconcile import FakeTransport, _task  # noqa: F401
