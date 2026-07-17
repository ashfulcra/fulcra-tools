"""Tracker-agnostic projection primitives for coord work records."""

from .ledger import BridgeLedger, LedgerEntry
from .model import (
    CapabilityState,
    Diagnostic,
    ManagedRecord,
    Snapshot,
    SourceIdentity,
    WorkRecord,
)
from .policy import Policy, load_policy
from .projection import Change, ChangeKind, Plan, build_plan

__all__ = [
    "BridgeLedger",
    "CapabilityState",
    "Change",
    "ChangeKind",
    "Diagnostic",
    "LedgerEntry",
    "ManagedRecord",
    "Plan",
    "Policy",
    "Snapshot",
    "SourceIdentity",
    "WorkRecord",
    "build_plan",
    "load_policy",
]

__version__ = "0.1.0"
