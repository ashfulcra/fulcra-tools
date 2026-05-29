"""Preferences tab name -> NSTabView index. Pure map so the 'open to
Annotations' deep link can't silently target the wrong tab."""
from fulcra_menubar.preferences.window import tab_index


def test_known_tabs():
    assert tab_index("plugins") == 0
    assert tab_index("annotations") == 1
    assert tab_index("notifications") == 2
    assert tab_index("about") == 3


def test_unknown_or_none_returns_none():
    assert tab_index(None) is None
    assert tab_index("bogus") is None
