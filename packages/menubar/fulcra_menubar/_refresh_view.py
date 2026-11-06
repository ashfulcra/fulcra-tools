"""Lazy AppKit host for a Python refresh callback shared by native views."""
_VIEW_TYPE = None


def refresh_view(frame):
    global _VIEW_TYPE
    if _VIEW_TYPE is None:
        from AppKit import NSView
        import objc

        class FulcraRefreshView(NSView):
            @objc.python_method
            def refresh(self):
                callback = getattr(self, '_refresh_callback', None)
                if callback is not None:
                    callback()

        _VIEW_TYPE = FulcraRefreshView
    return _VIEW_TYPE.alloc().initWithFrame_(frame)
