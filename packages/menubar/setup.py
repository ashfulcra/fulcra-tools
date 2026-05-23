"""py2app entry point.

    cd packages/menubar
    uv run python setup.py py2app

For development iteration ('alias' build that links to source rather
than copying):

    uv run python setup.py py2app -A
"""
from setuptools import setup

APP = ["fulcra_menubar/__main__.py"]
DATA_FILES = [
    ("fulcra_menubar/assets", [
        "fulcra_menubar/assets/menubar-icon.pdf",
    ]),
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
    ],
    "includes": [
        "AppKit", "Foundation", "Quartz", "ServiceManagement",
        "UserNotifications",
    ],
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
