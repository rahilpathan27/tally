"""Business dates and settlement cut-offs in Asia/Kolkata; timestamps are stored in UTC."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def business_date(moment: datetime, cutoff: time = time(0, 0)) -> date:
    """IST business date of ``moment``; activity at or after ``cutoff`` rolls to the next day."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    local = moment.astimezone(IST)
    shifted = local - timedelta(hours=cutoff.hour, minutes=cutoff.minute)
    rolled = shifted.date()
    return rolled + timedelta(days=1) if cutoff != time(0, 0) else rolled


def cutoff_instant(day: date, cutoff: time = time(0, 0)) -> datetime:
    """UTC instant at which business date ``day`` closes."""
    if cutoff == time(0, 0):
        closing = datetime.combine(day + timedelta(days=1), time(0, 0), IST)
    else:
        closing = datetime.combine(day, cutoff, IST)
    return closing.astimezone(UTC)


def last_closed_business_date(now: datetime, cutoff: time = time(0, 0)) -> date:
    """Most recent business date whose cut-off has passed at ``now``."""
    return business_date(now, cutoff) - timedelta(days=1)
