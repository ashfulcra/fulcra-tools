"""Preferences tab name -> NSTabView index. Pure map so the 'open to
Annotations' deep link can't silently target the wrong tab."""
from fulcra_menubar.preferences.window import WIDTH, HEIGHT, tab_index, tabview_frame


def test_tabview_fills_content_view_so_tab_strip_is_visible():
    """Regression: the NSTabView must fill the full content view (0,0,W,H).
    The old `HEIGHT - 22` bottom-anchored it and shoved the tab strip out of
    sight (empty grey band, no clickable Plugins/Annotations/… tabs)."""
    assert tabview_frame(WIDTH, HEIGHT) == (0.0, 0.0, WIDTH, HEIGHT)
    # The tab view height must equal the content height — never the buggy -22.
    assert tabview_frame(WIDTH, HEIGHT)[3] == HEIGHT
    assert tabview_frame(640.0, 480.0) == (0.0, 0.0, 640.0, 480.0)


def test_known_tabs():
    assert tab_index("plugins") == 0
    assert tab_index("annotations") == 1
    assert tab_index("notifications") == 2
    assert tab_index("about") == 3


def test_unknown_or_none_returns_none():
    assert tab_index(None) is None
    assert tab_index("bogus") is None
