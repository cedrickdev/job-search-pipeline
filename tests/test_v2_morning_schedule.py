"""A Swiss wall-clock schedule is evaluated at UTC instants, not server time."""

from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from backend.app.domain.identifiers import new_user_id
from backend.app.domain.morning_schedule import MorningSchedule, is_due


def schedule(*, at: time = time(8), cadence: str = "daily") -> MorningSchedule:
    return MorningSchedule(
        user_id=new_user_id(), enabled=True, local_time=at,
        cadence=cadence, updated_at=datetime(2026, 10, 12, tzinfo=UTC),
    )


def test_due_uses_zurich_wall_clock_not_utc_clock() -> None:
    morning = schedule()
    assert not is_due(morning, datetime(2026, 10, 12, 5, 59, tzinfo=UTC))
    assert is_due(morning, datetime(2026, 10, 12, 6, 0, tzinfo=UTC))


def test_weekday_cadence_skips_saturday_and_sunday() -> None:
    weekdays = schedule(cadence="weekdays")
    assert is_due(weekdays, datetime(2026, 10, 23, 7, 0, tzinfo=UTC))
    assert not is_due(weekdays, datetime(2026, 10, 24, 7, 0, tzinfo=UTC))
    assert not is_due(weekdays, datetime(2026, 10, 25, 7, 0, tzinfo=UTC))
    assert is_due(schedule(), datetime(2026, 10, 25, 7, 0, tzinfo=UTC))


def test_fallback_fold_checks_both_real_instants() -> None:
    early = schedule(at=time(2, 30))
    assert is_due(early, datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    assert is_due(early, datetime(2026, 10, 25, 1, 30, tzinfo=UTC))


def test_disabled_schedule_is_never_due() -> None:
    disabled = schedule().model_copy(update={"enabled": False})
    assert not is_due(disabled, datetime(2026, 10, 25, 7, 0, tzinfo=UTC))


@pytest.mark.parametrize("bad_time", ["25:00", time(8, 0, 1), time(8, tzinfo=UTC)])
def test_invalid_local_time_is_rejected(bad_time: str | time) -> None:
    with pytest.raises(ValidationError):
        schedule(at=bad_time)  # type: ignore[arg-type]


def test_invalid_cadence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(cadence="hourly")


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_due(schedule(), datetime(2026, 10, 12, 8))
