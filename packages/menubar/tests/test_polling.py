from __future__ import annotations

from fulcra_menubar.polling import PollingScheduler


def test_open_cadence_is_two_seconds():
    times: list[float] = []
    now = [0.0]

    def fake_monotonic() -> float:
        return now[0]

    def fake_sleep(s: float) -> None:
        now[0] += s

    def tick() -> None:
        times.append(now[0])

    sched = PollingScheduler(
        on_tick=tick, monotonic=fake_monotonic, sleep=fake_sleep,
    )
    sched.set_popover_open(True)

    stop_after = [3]

    def maybe_stop():
        if len(times) >= stop_after[0]:
            sched.stop()
    sched.add_post_tick_hook(maybe_stop)

    sched.run()

    assert times == [0.0, 2.0, 4.0]


def test_closed_cadence_is_ten_seconds():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(False)

    def maybe_stop():
        if len(times) >= 3:
            sched.stop()
    sched.add_post_tick_hook(maybe_stop)

    sched.run()

    assert times == [0.0, 10.0, 20.0]


def test_open_then_closed_switches_cadence():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(True)

    def maybe_stop():
        if len(times) == 2:
            sched.set_popover_open(False)
        if len(times) >= 4:
            sched.stop()

    sched.add_post_tick_hook(maybe_stop)
    sched.run()

    assert times == [0.0, 2.0, 12.0, 22.0]


def test_sleep_suspends_ticking():
    times: list[float] = []
    now = [0.0]

    sched = PollingScheduler(
        on_tick=lambda: times.append(now[0]),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    sched.set_popover_open(True)

    def maybe_act():
        if len(times) == 1:
            sched.suspend()
            now[0] += 100
            sched.resume()
        if len(times) >= 3:
            sched.stop()
    sched.add_post_tick_hook(maybe_act)

    sched.run()

    assert times == [0.0, 100.0, 102.0]
