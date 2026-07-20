"""coord-engine — the shared coord tool behind the fulcra-agent-* skills.

One stdlib-only engine wrapping a ``fulcra-agent-teams`` OKF namespace, exposing
``coord-engine`` subcommands the skills invoke (like ``fulcra-api``):

- ``reconcile`` / ``status`` / ``board`` / ``needs-me`` / ``search`` — self-healing
  ``task/index.md``+``log.md`` and queryable views (fulcra-agent-reconcile).
- ``roles status`` — the HELD/VACANT/CONTESTED + SLA fold (fulcra-agent-roles).
- ``task start`` / ``update`` / ``done`` — typed lifecycle + status machine
  (fulcra-agent-tasks).

Every stateful fold is here (deterministic + tested), never prose the agent eyeballs.
Design: ``docs/proposals/teams-convergence/``.
"""

__version__ = "1.6.10"
