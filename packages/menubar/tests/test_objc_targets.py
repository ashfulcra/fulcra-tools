"""Real AppKit target lifetime during repeated native row replacement."""
import pytest

pytest.importorskip('AppKit')
from AppKit import NSApplication, NSButton, NSView, NSMakeRect  # noqa: E402
from Foundation import NSObject  # noqa: E402
from fulcra_menubar import _objc_targets as targets  # noqa: E402


def test_replacing_rows_releases_callbacks_and_preserves_active_targets():
    NSApplication.sharedApplication()
    before = len(targets._retain)
    observed = []
    for page in range(20):
        row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 30))
        for number in range(10):
            button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
            targets.attach(button, lambda sender, value=(page, number): observed.append(value))
            row.addSubview_(button)
        assert len(targets._retain) == before + 10
        row.subviews()[0].target().call_(row.subviews()[0])
        targets.detach_view(row)
        assert len(targets._retain) == before
        assert all(button.target() is None for button in row.subviews())
    assert observed == [(page, 0) for page in range(20)]


def test_release_does_not_clear_targets_owned_by_other_native_components():
    NSApplication.sharedApplication()
    button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
    foreign = NSObject.alloc().init()
    button.setTarget_(foreign)
    targets.detach_view(button)
    assert button.target() == foreign
