from types import SimpleNamespace
from unittest.mock import patch

from fulcra_coord import listener_tick


def test_listener_tick_runs_forge_mirror_before_notify():
    calls = []

    def mirror(args, *, backend=None):
        calls.append(("mirror", args.repo, backend))
        return 0

    def notify(args, *, backend=None):
        calls.append(("notify", args.agent, backend))
        return 0

    args = SimpleNamespace(
        agent="codex:h:r", forge_mirror=True,
        repo="ashfulcra/fulcra-tools", format="table")
    with patch("fulcra_coord.listener_tick._forge_mirror.cmd_forge_mirror",
               side_effect=mirror), \
         patch("fulcra_coord.listener_tick._inbox.cmd_notify_inbox",
               side_effect=notify):
        assert listener_tick.cmd_listener_tick(args, backend=["fake"]) == 0

    assert calls == [
        ("mirror", "ashfulcra/fulcra-tools", ["fake"]),
        ("notify", "codex:h:r", ["fake"]),
    ]


def test_listener_tick_notifies_even_when_mirror_fails():
    calls = []

    def mirror(args, *, backend=None):
        calls.append("mirror")
        raise RuntimeError("forge unavailable")

    def notify(args, *, backend=None):
        calls.append("notify")
        return 0

    args = SimpleNamespace(agent="codex:h:r", forge_mirror=True,
                           repo=None, format="table")
    with patch("fulcra_coord.listener_tick._forge_mirror.cmd_forge_mirror",
               side_effect=mirror), \
         patch("fulcra_coord.listener_tick._inbox.cmd_notify_inbox",
               side_effect=notify):
        assert listener_tick.cmd_listener_tick(args, backend=["fake"]) == 0

    assert calls == ["mirror", "notify"]
