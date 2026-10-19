"""One account's Swiss morning-run clock; no queue or database side effects."""

from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import field_validator

from backend.app.domain.base import DomainModel, UtcDatetime
from backend.app.domain.identifiers import UserId

_ZURICH = ZoneInfo("Europe/Zurich")


class MorningSchedule(DomainModel):
    user_id: UserId
    enabled: bool
    local_time: time
    cadence: Literal["daily", "weekdays"]
    updated_at: UtcDatetime

    @field_validator("local_time")
    @classmethod
    def _minute_precision_wall_clock(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("local_time must be a naive HH:MM wall clock")
        return value


def is_due(schedule: MorningSchedule, instant: datetime) -> bool:
    """Whether this instant has reached the user's Swiss local run time.

    Duplicate same-day work is prevented by the queue's Swiss-date key, not by
    this predicate; both instants of a DST fold can legitimately be due.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    local = instant.astimezone(_ZURICH)
    return (
        schedule.enabled
        and (schedule.cadence == "daily" or local.weekday() < 5)
        and local.time().replace(tzinfo=None) >= schedule.local_time
    )
