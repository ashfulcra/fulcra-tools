from fulcra_menubar._humanize import humanize_minutes


def test_under_one_hour():
    assert humanize_minutes(30) == "30 minutes"


def test_one_hour():
    assert humanize_minutes(60) == "1 hour"


def test_exact_hours():
    assert humanize_minutes(360) == "6 hours"


def test_mixed_hours_minutes():
    assert humanize_minutes(90) == "1h 30m"


def test_one_day():
    assert humanize_minutes(1440) == "1 day"


def test_exact_days():
    assert humanize_minutes(2880) == "2 days"


def test_one_minute():
    assert humanize_minutes(1) == "1 minute"


def test_zero():
    assert humanize_minutes(0) == "0 minutes"
