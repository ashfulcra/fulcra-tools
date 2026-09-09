"""Report concurrent configuration edits to native controls."""
from fulcra_collect import config


def _show_conflict():
    from AppKit import NSAlert

    alert = NSAlert.alloc().init()
    alert.setMessageText_('Settings changed elsewhere')
    alert.setInformativeText_('Your change was not saved. Review the current settings and try again.')
    alert.addButtonWithTitle_('OK')
    alert.runModal()


def save(cfg):
    try:
        config.save(cfg)
    except config.ConfigConflictError:
        _show_conflict()
        return False
    return True
