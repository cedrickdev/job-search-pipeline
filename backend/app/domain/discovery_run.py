"""Safe, owner-scoped report of a saved-search collection run."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from backend.app.domain.base import CountryCode, DomainModel, ReasonCode, UtcDatetime
from backend.app.domain.identifiers import DiscoveryRunId, SearchProfileId, UserId


class DiscoveryRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DiscoveryTrigger(StrEnum):
    MANUAL = "MANUAL"
    MORNING = "MORNING"


class SourceHealthSummary(DomainModel):
    source_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    status: Literal["HEALTHY", "DEGRADED", "MISCONFIGURED", "UNAVAILABLE"]
    reason: ReasonCode | None = None


class DiscoveryRun(DomainModel):
    id: DiscoveryRunId
    user_id: UserId
    search_profile_id: SearchProfileId
    country: CountryCode
    trigger: DiscoveryTrigger
    status: DiscoveryRunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    seen_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    source_health: tuple[SourceHealthSummary, ...] = ()
    warning_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def _terminal_time_is_coherent(self) -> Self:
        if (self.status is DiscoveryRunStatus.RUNNING) != (self.finished_at is None):
            raise ValueError("only a running discovery run may lack finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("discovery finished_at cannot precede started_at")
        return self
