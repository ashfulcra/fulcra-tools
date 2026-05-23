# fulcra-menubar

macOS menubar UI for `fulcra-collect`. Python + PyObjC + rumps v1; a
Swift rewrite follows once the UX is locked (see
`docs/superpowers/specs/2026-05-22-fulcra-collect-menubar-design.md`).

## Run in dev mode

    cd /Users/Scanning/Developer/fulcra-tools
    uv sync --extra macos --package fulcra-menubar
    uv run --package fulcra-menubar python -m fulcra_menubar

The daemon must be running (`fulcra-collect service start`).

## Tests

    uv run pytest packages/menubar/tests/ -q

(Pure-model layer only — the view layer is manual smoke.)

## Build the .app

    uv sync --extra macos --extra build --package fulcra-menubar
    cd packages/menubar
    uv run python setup.py py2app

The unsigned `.app` lands in `packages/menubar/dist/Fulcra Collect.app`.
Code-signing and notarization land in sub-project 3.
