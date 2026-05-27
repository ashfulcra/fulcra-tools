# SP4: In-app docs link + Configure/Disable on popover rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close drift items D3 (no in-app docs link from menubar) and the second half of D4 (popover plugin rows lack Configure + Disable actions). Per user Q3 (docs open in browser at `/?route=docs`), Q5 (Configure always opens the web UI wizard), and Q7 ("?" icon in popover header next to gear).

**Architecture:** Three small surfaces of work. (1) Web UI gains a URL-param handler in `app.boot()` that reads `?route=...` and dispatches to the existing route system — enables menubar deep-linking. (2) Popover header gains a "?" button that opens the daemon's docs page in the system browser. (3) Popover plugin rows gain Configure (opens web UI wizard) and Disable (mirrors the Preferences tab's enable-switch flow) buttons.

**Tech Stack:** Python 3.12+ + PyObjC + rumps (menubar); vanilla JS + Alpine (web UI).

**Source spec:** `docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md` §SP4 (drift items D3 + D4-remainder). User Q3 + Q5 + Q7 answers.

**HEAD at plan start:** `086965e` (end of SP3).

**Reading list before starting:**
- `packages/web-ui/dist/static/app.js:79-205` — boot() + openSetupForPlugin (Task 1 surface).
- `packages/menubar/fulcra_menubar/popover/header.py` — current gear icon at lines 59-66 area (Task 2 surface).
- `packages/menubar/fulcra_menubar/popover/plugin_row.py` — row builder for Configure/Disable additions (Task 3 surface).
- `packages/menubar/fulcra_menubar/preferences/plugins_tab.py:279-290` — existing enable-switch flow Task 3's Disable button mirrors.

---

## File Structure

| File | Change | Why |
|---|---|---|
| `packages/web-ui/dist/static/app.js` | Modify | Add URL-param handler in `boot()` that reads `?route=docs` / `?route=configure&plugin=<id>` / `?route=settings` and routes accordingly. Clear URL params after consumption via `history.replaceState`. |
| `packages/menubar/fulcra_menubar/popover/header.py` | Modify | Add "?" docs button next to the gear icon. |
| `packages/menubar/fulcra_menubar/popover/plugin_row.py` | Modify | Add Configure (opens web UI URL) + Disable (cfg.disable + client.reload) buttons per row. |

---

## Task 1: Web UI URL-param handler

**Files:** Modify `packages/web-ui/dist/static/app.js`.

**Why:** The menubar's Configure/Disable affordances need to deep-link into the web UI's existing wizard route. Today the web UI has no URL-param routing — it starts at `route='loading'` and is routed only by `boot()`'s internal logic. Add a small handler in `boot()` that reads `URLSearchParams(window.location.search)` and dispatches.

- [ ] **Step 1: Find the boot() flow.**

```bash
sed -n '70,170p' packages/web-ui/dist/static/app.js
```

Read the existing boot() — auth check, route decision, etc.

- [ ] **Step 2: Add the URL-param handler.**

Inside `boot()`, AFTER the auth check completes (so unauthenticated users still see signin first), add:

```javascript
      // URL-param routing. Menubar deep-links here via URLs like:
      //   /?route=docs                — opens the in-app docs view
      //   /?route=configure&plugin=X  — opens the wizard for plugin X
      //   /?route=settings            — opens the Settings page
      // We consume the params and immediately clear them with
      // history.replaceState so a reload doesn't loop back here.
      // Added in SP4 (drift audit 2026-05-27) so the menubar
      // popover's '?' button and per-row Configure can land users
      // exactly where they need to be.
      const urlParams = new URLSearchParams(window.location.search);
      const requestedRoute = urlParams.get("route");
      if (requestedRoute) {
        // Strip the param so a refresh doesn't re-trigger.
        history.replaceState({}, "", window.location.pathname);

        if (requestedRoute === "docs") {
          this.route = "docs";
          // Default docs page; downstream tabs can navigate further.
          const docPage = urlParams.get("page") || "how-do-i-get-my-data";
          this.goToDocs(docPage, "");  // title resolved by daemon
          return;
        }
        if (requestedRoute === "configure") {
          const pluginId = urlParams.get("plugin");
          if (pluginId) {
            await this.openSetupForPlugin(pluginId);
            return;
          }
        }
        if (requestedRoute === "settings") {
          this.route = "settings";
          return;
        }
      }
```

Place this AFTER the existing auth + default-route logic so URL-param routes ONLY kick in when explicitly requested.

- [ ] **Step 3: Verify the existing `goToDocs` method exists with the expected signature.**

```bash
grep -n "goToDocs\b" packages/web-ui/dist/static/app.js packages/web-ui/dist/static/dashboard.js packages/web-ui/dist/index.html
```

If it's a method on `app()` that fetches the markdown, the call shape `this.goToDocs(name, title)` should work. If the signature differs (e.g., no title param), adjust.

If `goToDocs` doesn't exist on `app()` but on `dashboard()`, you may need to mirror its behaviour inline (fetch `/api/docs/{name}` and set `docsMarkdown` + route).

- [ ] **Step 4: `node --check` for syntax.**

```bash
node --check packages/web-ui/dist/static/app.js
```

Expected: exit 0.

- [ ] **Step 5: Smoke-test by visiting a URL.**

```bash
TOKEN=$(cat ~/.config/fulcra-collect/web-token)
# Verify the daemon serves the new app.js
curl -s "http://127.0.0.1:9292/static/app.js" | grep -c "URL-param routing"
```

Expected: returns at least 1 (the comment in your new code).

- [ ] **Step 6: Commit.**

```bash
git add packages/web-ui/dist/static/app.js
git commit -m "feat(web-ui): URL-param routing for menubar deep-links (SP4 task 1)

The menubar's '?' docs button and per-row Configure action (SP4
tasks 2-3) need to deep-link into the web UI. Today the web UI has
no URL-param routing — it routes only via boot()'s internal logic.

Add a small handler in app.boot() that reads URLSearchParams and
dispatches:
  /?route=docs              → docs view (with optional &page=NAME)
  /?route=configure&plugin=X → wizard for plugin X
  /?route=settings           → Settings page

History is cleared via replaceState so a refresh doesn't re-trigger
the URL params (otherwise the user would loop back to the same
deep-link on every reload).

The handler runs AFTER the auth check so unauthenticated users still
see signin first.

Refs SP4 D3 + D4 remainder, drift audit 2026-05-27."
```

---

## Task 2: Popover header "?" docs button

**Files:** Modify `packages/menubar/fulcra_menubar/popover/header.py`.

**Why:** Per Q3 + Q7 — small "?" icon next to the gear in the popover header. Opens the daemon's docs page in the system browser.

- [ ] **Step 1: Read the current header layout.**

```bash
sed -n '55,80p' packages/menubar/fulcra_menubar/popover/header.py
```

Find the gear button frame (likely at `x=330, y=30, w=20, h=20` per the scoping doc, but verify).

- [ ] **Step 2: Add the "?" button.**

Place it at `x=305, y=30, w=20, h=20` — sits 5pt to the LEFT of the gear (assuming gear at x=330) and within the 360pt popover width with comfortable margin.

```python
# "?" docs button — opens the daemon's in-app docs page in the
# system browser. Added in SP4 (drift audit 2026-05-27) so users
# can reach the data-sources docs without context-switching to
# the dashboard.
docs_btn = NSButton.alloc().initWithFrame_(NSMakeRect(305, 30, 20, 20))
docs_btn.setTitle_("?")
docs_btn.setBezelStyle_(NSBezelStyleCircular)  # or whatever circular bezel matches the gear
docs_btn.setToolTip_("Open docs in browser")

def _on_docs(_sender):
    # The daemon serves docs at /?route=docs (SP4 task 1).
    # Use subprocess.run(["open", url]) — macOS's standard
    # default-browser opener.
    import subprocess
    subprocess.run(
        ["open", "http://127.0.0.1:9292/?route=docs"],
        check=False,
    )

_attach(docs_btn, _on_docs)
view.addSubview_(docs_btn)
```

Adapt the bezel style + tooltip to match the file's existing conventions. The gear button next door is probably a small ratio-matched icon; use the same NSBezelStyle that the gear uses.

- [ ] **Step 3: Python syntax check.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/header.py').read())"
```

- [ ] **Step 4: Commit.**

```bash
git add packages/menubar/fulcra_menubar/popover/header.py
git commit -m "feat(menubar): '?' docs button in popover header (SP4 task 2)

Adds a small '?' button next to the existing gear icon in the popover
header. Click opens http://127.0.0.1:9292/?route=docs in the system
browser, where the daemon serves the in-app docs view added in
prior work (#51).

Per user Q3 + Q7 from the SP4 scoping pass: 'open in browser' over
'native markdown render' (cheap, full-fidelity, leverages daemon's
existing route) and 'header next to gear' over 'Preferences only'
(always one click away when a plugin fails and the user opens the
popover to investigate).

Requires SP4 task 1's URL-param handler in app.js so /?route=docs
actually routes.

Refs SP4 D3, drift audit 2026-05-27."
```

---

## Task 3: Popover plugin row Configure + Disable buttons

**Files:** Modify `packages/menubar/fulcra_menubar/popover/plugin_row.py`.

**Why:** Per Q5 (Configure always opens web UI) + the rest of D4 (popover lacks Configure + Disable). Mirrors the dashboard's `{Run now, Configure, Disable}` action set.

- [ ] **Step 1: Read the existing Run-now button + row layout.**

```bash
sed -n '70,130p' packages/menubar/fulcra_menubar/popover/plugin_row.py
```

Understand the right-side control layout. The row is 44pt tall. There's a Run-now button for manual + enabled-scheduled plugins.

- [ ] **Step 2: Add Configure button.**

After the existing Run-now button code, add a Configure button. Place it to the LEFT of Run-now. Width 80pt for "Configure". Adjust the Run-now button's x-coord if needed to make room.

```python
# Configure button — opens the web UI wizard for this plugin in
# the system browser. Per user Q5 from the SP4 scoping pass:
# 'always open the web UI wizard' rather than re-implementing the
# wizard in PyObjC. The wizard supports OAuth, file uploads,
# definition pickers, health checks, etc. — too complex to mirror
# natively.
configure_btn = NSButton.alloc().initWithFrame_(
    NSMakeRect(width - 200, 8, 76, 28)  # placement: adjust to fit
)
configure_btn.setTitle_("Configure")
configure_btn.setBezelStyle_(NSBezelStyleRounded)

def _on_configure(_sender, plugin_id=snap.id):
    import subprocess
    url = f"http://127.0.0.1:9292/?route=configure&plugin={plugin_id}"
    subprocess.run(["open", url], check=False)

_attach(configure_btn, _on_configure)
row.addSubview_(configure_btn)
```

- [ ] **Step 3: Add Disable button.**

Place to the LEFT of Configure. Width 64pt for "Disable". Only show when the plugin is currently enabled (mirror the Preferences tab's switch state).

```python
# Disable button — toggles plugin off via cfg.disable + client.reload,
# mirroring the Preferences tab's enable-switch flow (plugins_tab.py
# ~line 280-290). Only shown when the plugin is currently enabled —
# disabling a disabled plugin would be a no-op.
if snap.enabled:
    disable_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - 280, 8, 72, 28)
    )
    disable_btn.setTitle_("Disable")
    disable_btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_disable(_sender, plugin_id=snap.id):
        # Same flow Preferences uses: mutate local config object,
        # save, ask daemon to reload. The daemon will refuse to
        # schedule the plugin going forward.
        from .. import _config
        cfg = _config.load()
        cfg.disable(plugin_id)
        _config.save(cfg)
        client.reload()

    _attach(disable_btn, _on_disable)
    row.addSubview_(disable_btn)
```

The exact import path for `_config` depends on how it's imported in `plugins_tab.py` — match that.

- [ ] **Step 4: Verify layout doesn't break existing Run-now placement.**

The right-side controls now go: `Disable | Configure | Run now`. Make sure each button has enough x-coord room and the existing Run-now still appears at its expected position.

- [ ] **Step 5: Python syntax + tests.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/plugin_row.py').read())"
cd packages/menubar && uv run pytest -q 2>&1 | tail -3
```

Expected: 106 passing.

- [ ] **Step 6: Commit.**

```bash
git add packages/menubar/fulcra_menubar/popover/plugin_row.py
git commit -m "feat(menubar): popover row Configure + Disable buttons (SP4 task 3)

Adds two action buttons to each plugin row in the popover's
plugin-status view, mirroring the dashboard's {Run now, Configure,
Disable} action set:

  Configure — opens the web UI wizard for this plugin in the
              system browser (subprocess open
              http://127.0.0.1:9292/?route=configure&plugin=ID).
              Per user Q5: always open the web UI wizard rather
              than re-implement the wizard's 11+ step kinds
              (OAuth, file upload, definition picker, health
              check, etc.) in PyObjC.

  Disable   — toggles the plugin off via cfg.disable +
              client.reload, mirroring the Preferences tab's
              enable-switch flow. Only shown when the plugin is
              currently enabled (disabling a disabled plugin is
              a no-op).

Requires SP4 task 1's URL-param handler in app.js so the Configure
URL actually routes into the wizard.

Refs SP4 D4 remainder, drift audit 2026-05-27."
```

---

## Task 4: Rebuild menubar + manual verification

**Files:** none modified (verification + optional sweep follow-up).

- [ ] **Step 1: Reinstall menubar.**

```bash
uv tool install --force --editable packages/menubar \
  --with-editable packages/collect \
  --with-editable packages/fulcra-common \
  --with-editable packages/attention \
  --with-editable packages/media-helpers \
  --with-editable packages/dayone \
  --with-editable packages/csv-importer \
  --with rumps --with pyobjc-core \
  --with pyobjc-framework-Cocoa --with pyobjc-framework-UserNotifications \
  --with pyobjc-framework-ServiceManagement --with pyobjc-framework-Quartz 2>&1 | tail -3
```

- [ ] **Step 2: Restart daemon (Task 1's web-ui change requires it serving the new app.js, which the Cache-Control: no-cache fix from earlier in the session already ensures).**

```bash
launchctl kickstart -k gui/$(id -u)/com.fulcra.collect
sleep 3
pkill -f fulcra-menubar 2>/dev/null
sleep 1
fulcra-menubar 2>&1 >/dev/null &
disown
sleep 4
ps aux | grep -E "fulcra-menubar|com\.fulcra\.collect" | grep -v grep | head -4
```

- [ ] **Step 3: Verify the URL-param handler works.**

```bash
TOKEN=$(cat ~/.config/fulcra-collect/web-token)
# Confirm the new app.js handler is being served.
curl -s "http://127.0.0.1:9292/static/app.js" | grep -c "URL-param routing"
```

Expected: ≥1.

- [ ] **Step 4: Update running-process memory file.**

- [ ] **Step 5: Full pytest sweep.**

```bash
uv run --all-packages pytest -q packages/ 2>&1 | tail -3
```

Expected: 1451 passed (baseline, no new tests expected from SP4), 1 skipped.

- [ ] **Step 6: Orphan/obsolete sweep.**

```bash
git diff 086965e..HEAD --stat
grep -rn "Configure button\|Disable button\|docs link" packages/menubar/
```

If stale references exist (e.g., a comment saying "no Configure on popover"), update them. Commit follow-up if anything's found.

- [ ] **Step 7: Surface manual walkthrough.**

User verifies:

A. **Popover header**: "?" icon visible next to gear. Click opens the daemon's docs page in your default browser. Should land on `/how-do-i-get-my-data` per the default page.

B. **Popover plugin row (View Status →)**:
- Enabled plugins show three buttons in the right column: Disable | Configure | Run now.
- Disabled plugins show just Configure (no Disable, no Run-now).
- Click Configure → web UI opens at `/?route=configure&plugin=<id>` and lands on the wizard for that plugin.
- Click Disable → switch in Preferences should now show disabled state; popover refresh removes the Disable button (now showing only Configure for a disabled plugin).

C. **Web UI URL params**:
- Visit `http://127.0.0.1:9292/?route=docs` directly → lands on docs view.
- Visit `http://127.0.0.1:9292/?route=settings` → lands on Settings.
- Visit `http://127.0.0.1:9292/?route=configure&plugin=lastfm` → lands on Last.fm's wizard.
- After any of these, the URL bar should show just `http://127.0.0.1:9292/` (the replaceState).

D. **No regressions**: SP1/SP2/SP3 surfaces still work.

---

## Final cross-cutting code review

After all 4 tasks land, dispatch `superpowers:code-reviewer` over the combined diff `086965e..HEAD`. Cover:
- Web UI URL-param handler doesn't break unauthenticated flows.
- replaceState correctly strips params so reload doesn't loop.
- Menubar's Configure/Disable buttons placement doesn't collide with existing Run-now positioning.
- "?" icon visual coherence with the existing gear icon.

## Acceptance

- [ ] `?route=docs` / `?route=configure&plugin=X` / `?route=settings` URLs work.
- [ ] Popover header "?" icon visible, opens docs.
- [ ] Popover plugin rows show Disable | Configure | Run now action set.
- [ ] Disable removes Disable button (row refreshes to disabled state).
- [ ] No regressions in tests or earlier sub-projects.
