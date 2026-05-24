"""Pure functions for humanizing durations and timestamps."""


def humanize_minutes(minutes: int) -> str:
    """Convert a minute count to a human-readable string.

    Examples:
        0 → '0 minutes'
        1 → '1 minute'
        30 → '30 minutes'
        60 → '1 hour'
        360 → '6 hours'
        90 → '1h 30m'
        1440 → '1 day'
        2880 → '2 days'
    """
    if minutes == 0:
        return "0 minutes"
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes // 60}h {minutes % 60}m"
