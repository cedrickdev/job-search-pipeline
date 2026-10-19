"""Owner-scoped, reversible Morning Run checkpoints; never submission authority."""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from backend.app.domain.base import DomainModel, ReasonCode, UtcDatetime
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId


class PreparationStage(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    CV_RENDERED = "CV_RENDERED"
    DOCUMENTS_RENDERED = "DOCUMENTS_RENDERED"
    SKIPPED = "SKIPPED"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    FAILED = "FAILED"


_STOPPED = frozenset({PreparationStage.SKIPPED, PreparationStage.REQUIRES_HUMAN,
                      PreparationStage.FAILED, PreparationStage.DOCUMENTS_RENDERED})


class Preparation(DomainModel):
    user_id: UserId
    discovery_run_id: DiscoveryRunId
    opportunity_id: OpportunityId
    stage: PreparationStage
    decision_id: UUID | None = None
    resume_version_id: UUID | None = None
    cover_letter_version_id: UUID | None = None
    reason_code: ReasonCode | None = None
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.stage in {PreparationStage.CV_RENDERED,
                          PreparationStage.DOCUMENTS_RENDERED} and self.resume_version_id is None:
            raise ValueError("a CV checkpoint requires an exact version")
        if self.stage is PreparationStage.DOCUMENTS_RENDERED \
                and self.cover_letter_version_id is None:
            raise ValueError("document readiness requires both versions")
        if self.cover_letter_version_id is not None and self.resume_version_id is None:
            raise ValueError("a letter checkpoint requires a CV checkpoint")
        if self.stage in {PreparationStage.SKIPPED, PreparationStage.REQUIRES_HUMAN,
                          PreparationStage.FAILED} and self.reason_code is None:
            raise ValueError("a stopped preparation requires a reason code")
        return self

    def advance(self, stage: PreparationStage, *, at: datetime,
                reason_code: str | None = None) -> Self:
        if self.stage in _STOPPED or at < self.updated_at:
            raise ValueError("a terminal or older preparation cannot advance")
        allowed = {
            PreparationStage.DISCOVERED: {PreparationStage.MATCHED,
                                          PreparationStage.SKIPPED,
                                          PreparationStage.REQUIRES_HUMAN,
                                          PreparationStage.FAILED},
            PreparationStage.MATCHED: {PreparationStage.CV_RENDERED,
                                       PreparationStage.SKIPPED,
                                       PreparationStage.REQUIRES_HUMAN,
                                       PreparationStage.FAILED},
            PreparationStage.CV_RENDERED: {PreparationStage.DOCUMENTS_RENDERED,
                                           PreparationStage.REQUIRES_HUMAN,
                                           PreparationStage.FAILED},
        }
        if stage not in allowed.get(self.stage, set()):
            raise ValueError("invalid preparation stage transition")
        return type(self).model_validate({**self.model_dump(), "stage": stage,
                                          "reason_code": reason_code,
                                          "updated_at": at})

    def record_document(self, document_type: str, version_id: UUID, *,
                        at: datetime) -> Self:
        if at < self.updated_at:
            raise ValueError("an older checkpoint cannot replace a newer one")
        if document_type == "RESUME":
            if self.resume_version_id == version_id:
                return self
            if self.stage is not PreparationStage.MATCHED or self.resume_version_id:
                raise ValueError("CV already checkpointed or preparation not matched")
            return type(self).model_validate({**self.model_dump(),
                                              "stage": PreparationStage.CV_RENDERED,
                                              "resume_version_id": version_id,
                                              "updated_at": at})
        if document_type == "COVER_LETTER":
            if self.cover_letter_version_id == version_id:
                return self
            if self.stage is not PreparationStage.CV_RENDERED \
                    or self.cover_letter_version_id:
                raise ValueError("letter already checkpointed or CV missing")
            return type(self).model_validate({**self.model_dump(),
                                              "stage": PreparationStage.DOCUMENTS_RENDERED,
                                              "cover_letter_version_id": version_id,
                                              "updated_at": at})
        raise ValueError("unsupported document type")
