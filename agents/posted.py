"""
When a posting went up, as a plain ISO date - one parser for every source.

Boards disagree on the shape: Greenhouse and Ashby send ISO timestamps with an
offset, Lever sends epoch milliseconds, JobSpy a date, Built In a datetime
parsed from a "3 Days Ago" badge. Everything lands here and comes out as
"YYYY-MM-DD", or None.

An unparseable value is None, never a guess. The tracker keeps estimates out
of Date Posted entirely: where no board date exists, the page falls back to
Date Opened (when the tracker first saw the role) and labels it as such.
"""
from datetime import date, datetime, timezone

# Anything before this is a parse accident (an epoch in seconds read as ms, a
# zero timestamp), not a real posting.
_EARLIEST = date(2000, 1, 1)


def to_iso_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value     # Lever: epoch ms
        try:
            day = datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if text.lower() in ("nan", "nat", "none", "null"):
            return None
        if text.isdigit():
            return to_iso_date(int(text))
        try:
            day = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                day = date.fromisoformat(text[:10])
            except ValueError:
                return None
    if day < _EARLIEST or day > date.today():
        return None
    return day.isoformat()


def days_since(iso_day, today=None):
    """Whole days from iso_day to today, or None."""
    if not iso_day:
        return None
    try:
        then = date.fromisoformat(str(iso_day)[:10])
    except ValueError:
        return None
    return max(((today or date.today()) - then).days, 0)
