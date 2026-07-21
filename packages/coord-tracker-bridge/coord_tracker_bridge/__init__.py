"""Tracker-agnostic projection primitives for coord work records."""

from .ledger import BridgeLedger, LedgerEntry
from .lease import FileLease, LeaseHeld
from .linear import (
    GraphQLResponse,
    LinearClient,
    LinearError,
    LinearTrackerAdapter,
    ResourceMissing,
    ResourcePlan,
)
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
from .service import BridgePlan, BridgeService, SyncResult
from .source import EngineSourceAdapter, FulcraTeamsTransport, TeamsSourceAdapter, TeamsTransportError

__all__ = [
    "BridgeLedger",
    "BridgePlan",
    "BridgeService",
    "CapabilityState",
    "Change",
    "ChangeKind",
    "Diagnostic",
    "EngineSourceAdapter",
    "FileLease",
    "FulcraTeamsTransport",
    "GraphQLResponse",
    "LedgerEntry",
    "LeaseHeld",
    "LinearClient",
    "LinearError",
    "LinearTrackerAdapter",
    "ManagedRecord",
    "Plan",
    "Policy",
    "ResourceMissing",
    "ResourcePlan",
    "Snapshot",
    "SourceIdentity",
    "SyncResult",
    "TeamsSourceAdapter",
    "TeamsTransportError",
    "WorkRecord",
    "build_plan",
    "load_policy",
]

__version__ = "0.4.0"
