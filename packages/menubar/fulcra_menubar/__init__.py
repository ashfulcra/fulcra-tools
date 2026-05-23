"""Fulcra Collect — macOS menubar UI (Python + PyObjC + rumps).

This package is the v1 of sub-project 2 of the fulcra-collect roadmap.
The UI sits on top of the fulcra-collect daemon as a thin JSON-over-UDS
client; everything plugin-side stays on the daemon.

The pure-model layer (daemon_client, model, polling, notifications)
imports no PyObjC and is fully unit-testable on any platform. The view
layer (status_item, popover, preferences) is macOS-only and exercised
by manual smoke.
"""
