"""Per-area HTTP route modules.

Each module exposes a ``register(app, ctx)`` function that registers its
routes on the given FastAPI app. ``ctx`` is a :class:`RouteContext` carrying
shared dependencies (the daemon, the auth dependency, the Fulcra client
factory, etc.). The orchestration lives in :mod:`fulcra_collect.web`.
"""
