"""Charge an atomic daily budget before each reviewed Swiss ATS fetch."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from backend.app.discovery.adapters.v1_sources import (
    Clock,
    PackResolver,
    V1AtsSourceAdapter,
    V1FetchCallable,
    utc_now,
)
from backend.app.discovery.ats_board_catalog import ReviewedBoard
from backend.app.discovery.contracts import (
    DiscoveryMetrics,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryWarning,
    DiscoveryWarningCode,
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
    SourceMetadata,
)
from backend.app.domain.opportunity import Opportunity
from pipeline.http_fetch import SOURCE_RETRIES_OVERRIDE

_ZURICH = ZoneInfo("Europe/Zurich")


class AtsRequestBudget(Protocol):
    async def reserve(self, source_key: str, board_token: str,
                      swiss_date: date, limit: int) -> bool: ...


class BudgetedAtsSourceAdapter:
    """Compose one-board V1 adapters, never fetching without a reservation."""

    def __init__(self, *, metadata: SourceMetadata, fetch: V1FetchCallable,
                 boards: Sequence[ReviewedBoard], budget: AtsRequestBudget,
                 packs: PackResolver, clock: Clock = utc_now) -> None:
        self._metadata = metadata
        self._boards = tuple(boards)
        self._budget = budget
        self._clock = clock
        self._adapters = tuple(V1AtsSourceAdapter(
            metadata=metadata, fetch=fetch, boards=(board,), packs=packs, clock=clock)
            for board in self._boards)

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    @property
    def boards(self) -> tuple[ReviewedBoard, ...]:
        return self._boards

    def _failed(self, reason: SourceFailureCode, checked_at: datetime) -> SourceHealth:
        detail = ("daily ATS request budget exhausted" if reason is
                  SourceFailureCode.SOURCE_BUDGET_EXHAUSTED else
                  "ATS request budget unavailable")
        return SourceHealth(source_key=self.metadata.source_key,
                            status=SourceHealthStatus.UNAVAILABLE,
                            checked_at=checked_at, reason=reason, detail=detail)

    async def _reserve(self, board: ReviewedBoard) -> SourceFailureCode | None:
        swiss_day = self._clock().astimezone(_ZURICH).date()
        try:
            allowed = await self._budget.reserve(
                self.metadata.source_key, board.token, swiss_day,
                board.request_budget_per_day)
        except Exception:
            # No DB exception text, query parameter or token reaches user-visible health.
            return SourceFailureCode.SOURCE_BUDGET_UNAVAILABLE
        return None if allowed else SourceFailureCode.SOURCE_BUDGET_EXHAUSTED

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if not self._boards:
            return DiscoveryResult(health=SourceHealth(
                source_key=self.metadata.source_key,
                status=SourceHealthStatus.HEALTHY, checked_at=self._clock()))
        results: list[DiscoveryResult] = []
        warnings: list[DiscoveryWarning] = []
        failures: list[SourceHealth] = []
        for board, adapter in zip(self._boards, self._adapters, strict=True):
            reason = await self._reserve(board)
            if reason is not None:
                failures.append(self._failed(reason, self._clock()))
                warnings.append(DiscoveryWarning(
                    code=DiscoveryWarningCode(reason.value),
                    detail="ATS employer board was not fetched: request budget gate",
                    source_key=self.metadata.source_key))
                continue
            # V1 fetch retries internally. One durable reservation authorizes
            # one HTTP attempt, so disable those retries only for this V2 call.
            retry_token = SOURCE_RETRIES_OVERRIDE.set(0)
            try:
                result = await adapter.discover(request)
            finally:
                SOURCE_RETRIES_OVERRIDE.reset(retry_token)
            results.append(result)
            warnings.extend(result.warnings)
            if not result.is_usable:
                failures.append(result.health)

        usable = [result for result in results if result.is_usable]
        if not usable:
            health = failures[0] if failures else SourceHealth(
                source_key=self.metadata.source_key,
                status=SourceHealthStatus.HEALTHY, checked_at=self._clock())
        elif failures:
            health = SourceHealth(source_key=self.metadata.source_key,
                                  status=SourceHealthStatus.DEGRADED,
                                  checked_at=self._clock(),
                                  reason=SourceFailureCode.SOURCE_PARTIAL_FAILURE,
                                  detail="one or more ATS employer boards were not fetched")
        else:
            health = SourceHealth(source_key=self.metadata.source_key,
                                  status=SourceHealthStatus.HEALTHY,
                                  checked_at=self._clock())
        seen: dict[object, Opportunity] = {}
        for result in usable:
            for opportunity in result.opportunities:
                seen.setdefault(opportunity.id, opportunity)
        opportunities = tuple(seen.values())[:request.limit]
        if len(seen) > request.limit:
            warnings.append(DiscoveryWarning(
                code=DiscoveryWarningCode.LIMIT_TRUNCATED,
                detail="ATS results exceeded the requested limit",
                source_key=self.metadata.source_key))
        requests_made = sum(result.metrics.requests_made for result in results)
        postings_seen = sum(result.metrics.postings_seen for result in results)
        skipped = sum(result.metrics.postings_skipped for result in usable)
        return DiscoveryResult(
            health=health, opportunities=opportunities,
            warnings=tuple(warnings),
            metrics=DiscoveryMetrics(
                requests_made=requests_made, postings_seen=postings_seen,
                postings_skipped=skipped,
                opportunities_returned=len(opportunities)),
        )

    async def healthcheck(self) -> SourceHealth:
        if not self._boards:
            return SourceHealth(source_key=self.metadata.source_key,
                                status=SourceHealthStatus.HEALTHY,
                                checked_at=self._clock())
        reason = await self._reserve(self._boards[0])
        if reason is not None:
            return self._failed(reason, self._clock())
        retry_token = SOURCE_RETRIES_OVERRIDE.set(0)
        try:
            return await self._adapters[0].healthcheck()
        finally:
            SOURCE_RETRIES_OVERRIDE.reset(retry_token)
