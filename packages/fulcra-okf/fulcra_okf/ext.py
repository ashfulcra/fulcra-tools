"""Fulcra extension-key namespacing + the upstream-contribution registry.

Unknown keys are preserved by OKF consumers (spec §9), so x_fulcra_* keys are our staging
ground for fields OKF does not yet express. Each entry maps a prefixed key to the standard
name we propose upstream and its status.
"""
from __future__ import annotations

from dataclasses import dataclass

NAMESPACE = "x_fulcra_"


@dataclass(frozen=True)
class ExtField:
    proposed: str   # the standard field name we propose upstream
    status: str     # "proposed" | "accepted" | "standard"


def namespaced(name: str) -> str:
    return name if name.startswith(NAMESPACE) else NAMESPACE + name


def is_namespaced(key: str) -> bool:
    return key.startswith(NAMESPACE)


REGISTRY: dict[str, ExtField] = {
    "x_fulcra_consent_audience": ExtField(proposed="consent_audience", status="proposed"),
    "x_fulcra_weight": ExtField(proposed="weight", status="proposed"),
    "x_fulcra_signal_id": ExtField(proposed="id", status="proposed"),
    "x_fulcra_decay": ExtField(proposed="decay", status="proposed"),
}
