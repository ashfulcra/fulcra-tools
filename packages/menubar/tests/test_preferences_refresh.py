"""Cached Preferences reopens must reload the current account's choices."""
import ast
from pathlib import Path
from types import SimpleNamespace


def test_cached_preferences_reopen_refreshes_instead_of_reusing_failed_or_old_account_data():
    source = Path(__file__).parents[1] / 'fulcra_menubar' / 'app.py'
    outer = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.ClassDef)
                 and node.name == 'FulcraMenubarApp')
    method = next(node for node in outer.body if isinstance(node, ast.FunctionDef) and node.name == '_open_prefs')
    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None
    module = StripImports().visit(ast.Module(body=[method], type_ignores=[]))
    ast.fix_missing_locations(module)
    events = []
    controller = SimpleNamespace(select_tab=lambda name: events.append(('select', name)),
        refresh_annotations=lambda: events.append(('refresh',)),
        window=lambda: SimpleNamespace(makeKeyAndOrderFront_=lambda _: events.append(('show',))))
    namespace = {'NSApp': SimpleNamespace(activateIgnoringOtherApps_=lambda _: None)}
    exec(compile(module, str(source), 'exec'), namespace)
    owner = SimpleNamespace(_prefs_controller=controller)
    namespace['_open_prefs'](owner)
    namespace['_open_prefs'](owner, 'annotations')
    assert events.count(('refresh',)) == 2
    assert events.count(('show',)) == 2


def test_real_tab_delegate_refreshes_when_annotations_is_selected():
    import pytest
    pytest.importorskip('AppKit')
    from AppKit import NSApplication, NSWindow, NSTabView, NSTabViewItem, NSView, NSMakeRect
    from fulcra_menubar.preferences.window import PreferencesController
    NSApplication.sharedApplication()
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 100, 100), 0, 2, False)
    controller = PreferencesController.alloc().initWithWindow_(window)
    tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
    for label in ('Plugins', 'Annotations'):
        item = NSTabViewItem.alloc().initWithIdentifier_(label)
        item.setLabel_(label)
        item.setView_(NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 80)))
        tabs.addTabViewItem_(item)
    observed = []
    controller._annotations_view = SimpleNamespace(refresh=lambda: observed.append('refresh'))
    controller._tabs = tabs
    tabs.setDelegate_(controller)
    try:
        controller.select_tab('annotations')
        assert observed == ['refresh']
        controller.refresh_annotations()
        assert observed == ['refresh', 'refresh']
    finally:
        tabs.setDelegate_(None)
