from __future__ import annotations

from fulcra_menubar.notifications import NotificationCentre


def test_one_post_per_plugin_per_hour():
    posts = []
    now = [0.0]
    centre = NotificationCentre(
        post=lambda title, body: posts.append((now[0], title, body)),
        monotonic=lambda: now[0],
    )

    centre.notify_failure("lastfm", "401 unauthorized")
    centre.notify_failure("lastfm", "401 unauthorized")
    now[0] += 3600 - 1
    centre.notify_failure("lastfm", "401 unauthorized")
    now[0] += 2
    centre.notify_failure("lastfm", "401 unauthorized")

    assert [t for t, _, _ in posts] == [0.0, 3601.0]


def test_different_plugins_are_independent():
    posts = []
    centre = NotificationCentre(
        post=lambda title, body: posts.append((title, body)),
        monotonic=lambda: 0.0,
    )

    centre.notify_failure("lastfm", "x")
    centre.notify_failure("spotify-extended", "y")

    assert len(posts) == 2


def test_mute_all_suppresses_everything():
    posts = []
    centre = NotificationCentre(
        post=lambda title, body: posts.append((title, body)),
        monotonic=lambda: 0.0,
    )
    centre.mute_all = True

    centre.notify_failure("lastfm", "x")
    centre.notify_daemon_stopped()

    assert posts == []


def test_daemon_stopped_is_independent_of_plugin_dedup():
    posts = []
    now = [0.0]
    centre = NotificationCentre(
        post=lambda title, body: posts.append((now[0], title)),
        monotonic=lambda: now[0],
    )

    centre.notify_failure("lastfm", "x")
    centre.notify_daemon_stopped()
    centre.notify_daemon_stopped()
    now[0] += 3601
    centre.notify_daemon_stopped()

    assert [t for t, _ in posts] == [0.0, 0.0, 3601.0]
