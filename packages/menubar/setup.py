"""py2app entry point.

    cd packages/menubar
    uv run python setup.py py2app

For development iteration ('alias' build that links to source rather
than copying):

    uv run python setup.py py2app -A

The bundle ships the full daemon, not just the menubar: every collect
plugin (discovered at runtime via the static manifest, since
entry-point metadata doesn't survive the freeze), the FastAPI/uvicorn
web server, and the web-ui SPA + in-app docs copied under
Contents/Resources where fulcra_collect._resources looks for them.
"""
import os

from setuptools import setup

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _tree(rel_src, rel_dest):
    """Enumerate every file under <repo>/<rel_src> as py2app
    (dest_subdir, [files]) tuples, preserving subdirectories. py2app
    needs explicit file lists; it won't recurse a directory for us."""
    out = []
    base = os.path.join(_REPO, rel_src)
    for root, _dirs, files in os.walk(base):
        if not files:
            continue
        rel = os.path.relpath(root, base)
        dest = rel_dest if rel == "." else os.path.join(rel_dest, rel)
        out.append((dest, [os.path.join(root, f) for f in files]))
    return out


APP = ["fulcra_menubar/__main__.py"]

DATA_FILES = [
    ("fulcra_menubar/assets", [
        "fulcra_menubar/assets/menubar-icon.png",
    ]),
    # SPA + in-app docs under Contents/Resources so the frozen daemon's
    # _resources.frontend_dir()/docs_dir() resolve them.
    *_tree("packages/web-ui/dist", "web-ui/dist"),
    ("docs", [os.path.join(_REPO, "docs", "how-do-i-get-my-data.md")]),
]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Fulcra Collect",
        "CFBundleDisplayName": "Fulcra Collect",
        "CFBundleIdentifier": "com.fulcradynamics.collect.menubar",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,                # menubar app — no dock icon
        "NSHumanReadableCopyright": "Fulcra Dynamics",
    },
    "packages": [
        "rumps", "fulcra_collect", "fulcra_menubar", "tomlkit", "keyring",
        # Plugin packages — found at runtime via the static manifest
        # (their entry-point metadata doesn't survive the freeze).
        "fulcra_media", "fulcra_dayone", "fulcra_attention",
        "fulcra_common", "fulcra_csv",
        # Daemon web server.
        "fastapi", "starlette", "pydantic", "pydantic_core", "uvicorn",
        "httpx",
    ],
    "includes": [
        "AppKit", "Foundation", "Quartz", "ServiceManagement",
        "UserNotifications",
        # uvicorn loads these dynamically; modulegraph can't see them.
        # (The full set is finalized against runtime errors in the
        # build/verify task.)
        "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
