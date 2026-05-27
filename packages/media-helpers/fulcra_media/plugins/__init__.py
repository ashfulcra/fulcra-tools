"""Per-plugin modules for fulcra-media-helpers.

Each module here exposes a single ``PLUGIN`` constant — the same convention
the sibling ``fulcra-attention`` and ``fulcra-dayone`` packages use. The
``fulcra_media.collect_plugins`` module re-exports each plugin under its
historical ``<NAME>_PLUGIN`` alias for back-compat with the test suite and
any older code that imports directly from the old monolith.
"""
