"""The small, internal-only HTTP contract between API and terminal manager."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CreateTerminalRuntimeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    expires_at: datetime


class CreateTerminalRuntimeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    runtime_reference: str
