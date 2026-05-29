#!/usr/bin/env bash
# Prove the built .app is self-contained: run ONLY its bundled app_packages
# (no dev workspace on path, -S to skip site/editable .pth) and confirm the
# daemon discovers all plugins and resolves its bundled web-UI + docs.
set -euo pipefail
cd "$(dirname "$0")/../../.."                 # repo root
APPP="$PWD/packages/menubar/build/fulcra-menubar/macos/app/Fulcra Collect.app/Contents/Resources/app_packages"
test -d "$APPP" || { echo "FAIL: build first (build_macos_app.sh)"; exit 1; }

PYTHONPATH="$APPP" uv run python -S - <<'PY'
from fulcra_collect import registry, _resources
assert "app_packages" in registry.__file__, "not loading from the bundle"
assert _resources.is_frozen(), "bundle should report frozen"
r = registry.discover()
assert len(r.plugins) >= 17 and not r.errors, f"plugins={len(r.plugins)} errors={r.errors}"
assert (_resources.frontend_dir() / "index.html").is_file(), "web-ui SPA missing"
assert (_resources.docs_dir() / "how-do-i-get-my-data.md").is_file(), "docs page missing"
print(f"BUNDLE OK — {len(r.plugins)} plugins, web-UI + docs resolved, frozen.")
PY
