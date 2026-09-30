from datetime import UTC, date, datetime, time

import pytest
from libs.common.business_time import (
    business_date,
    cutoff_instant,
    last_closed_business_date,
)


def test_ist_midnight_boundary() -> None:
    # 18:29:59 UTC is 23:59:59 IST; 18:30 UTC is the next IST day.
    assert business_date(datetime(2026, 9, 30, 18, 29, 59, tzinfo=UTC)) == date(2026, 9, 30)
    assert business_date(datetime(2026, 9, 30, 18, 30, tzinfo=UTC)) == date(2026, 10, 1)


def test_afternoon_cutoff_rolls_late_activity_forward() -> None:
    cutoff = time(16, 0)
    # 10:29 UTC = 15:59 IST (before cut-off) and 10:30 UTC = 16:00 IST (after).
    assert business_date(datetime(2026, 9, 30, 10, 29, tzinfo=UTC), cutoff) == date(2026, 9, 30)
    assert business_date(datetime(2026, 9, 30, 10, 30, tzinfo=UTC), cutoff) == date(2026, 10, 1)
    assert cutoff_instant(date(2026, 9, 30), cutoff) == datetime(2026, 9, 30, 10, 30, tzinfo=UTC)


def test_last_closed_date_and_naive_rejection() -> None:
    assert last_closed_business_date(datetime(2026, 9, 30, 20, 0, tzinfo=UTC)) == date(2026, 9, 30)
    assert cutoff_instant(date(2026, 9, 30)) == datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        business_date(datetime(2026, 9, 30, 12, 0))
