"""`AccountExport` — the lifecycle of one request to hand a user their own data (§23-25).

An account export is the record of "give me a portable copy of everything you hold about
me". It is deliberately *not* the data itself: the archive lives in an export store behind
an abstraction (§25), and this model tracks only where that archive is, how big it is, and
when it stops being available. Keeping the bytes out of the domain is what lets production
object storage replace the local development store without the domain noticing.

Three rules shape it, and each protects a promise the export feature makes:

- **The state machine is closed and coherent.** `AccountExportStatus` is the four states an
  export can be in — pending, ready, failed, expired — and a `@model_validator` refuses any
  combination of fields that does not match the state. A `READY` export without a stored
  artifact, or a `FAILED` one without a reason, cannot be constructed, so a caller can trust
  that a `READY` export is downloadable and a `FAILED` one explains itself.
- **Availability is derived, never a stored flag a clock could contradict.** `is_downloadable`
  and `is_expired` take the instant to compare against, like every time-dependent domain
  method, so a `READY` export whose retention window has elapsed reads as not downloadable the
  moment it lapses — even before a retention sweep (Phase 16 §30) has flipped its status to
  `EXPIRED`. Expiry is fail-safe: the model stops offering the archive before the job deletes
  it, never after.
- **Nothing here holds a secret.** The export *store* holds bytes this model only points at by
  `storage_key`; the exclusion of secrets from those bytes is the gatherer's and service's job
  (§24). This model carries no credential, no digest, no ciphertext — only metadata about an
  archive.

Pure domain value: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The export service (Phase 16 §23-25) drives the transitions; this
module knows nothing of filesystems, object storage or JSON serialization.
"""
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, ReasonCode, UtcDatetime
from backend.app.domain.identifiers import AccountExportId, UserId

# The schema version the current code writes an export envelope under. Stamped on every export
# so an archive produced by an older build is self-describing when a newer one reads it back.
# Bumped when the export payload's shape changes in a way a consumer must notice.
ACCOUNT_EXPORT_SCHEMA_VERSION = 1


class AccountExportStatus(StrEnum):
    """The lifecycle of one export request — the closed set the state machine allows.

    - `PENDING` — requested, no archive produced yet; every artifact field is unset;
    - `READY` — the archive is stored and downloadable until it expires; `storage_key`,
      `byte_size`, `completed_at` and `expires_at` are all set and no failure is recorded;
    - `FAILED` — production failed; `failure_reason` and `completed_at` are set and no artifact
      exists (a failed export never leaves a half-written archive behind);
    - `EXPIRED` — the archive's retention window elapsed and a retention sweep purged the bytes;
      `completed_at` and `expires_at` remain as provenance but `storage_key`/`byte_size` are
      cleared, because the artifact is gone.
    """

    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class AccountExport(DomainModel):
    """One account-export request and the archive it produced (§23-25).

    User-owned like every entity, read `WHERE user_id = ?`. It names the export `schema_version`
    the archive was written under, its `status`, and — once `READY` — the `storage_key` the
    export store can resolve to bytes plus the `byte_size` of those bytes. `completed_at` is when
    production finished (ready or failed); `expires_at` is when the archive stops being offered
    and becomes eligible for the retention sweep; `failure_reason` is the machine code for why a
    `FAILED` export failed. The model holds no bytes and no secret — only where the archive is
    and when it lapses.

    Transitions produce new validated instances (`completed`, `failed`, `expired`) rather than
    mutating in place, because a `DomainModel` is frozen; each runs the coherence validator, so
    an incoherent state cannot be reached by any path.
    """

    id: AccountExportId
    user_id: UserId
    status: AccountExportStatus
    schema_version: int = Field(ge=1)
    storage_key: NonEmptyStr | None = None
    byte_size: int | None = Field(default=None, ge=0)
    completed_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    failure_reason: ReasonCode | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _state_and_fields_are_coherent(self) -> Self:
        has_artifact = self.storage_key is not None and self.byte_size is not None
        if (self.storage_key is None) != (self.byte_size is None):
            raise ValueError(
                "an AccountExport artifact is both-or-neither: storage_key and byte_size are "
                "set together or both left unset")
        if self.status is AccountExportStatus.PENDING:
            if has_artifact or self.completed_at is not None \
                    or self.expires_at is not None or self.failure_reason is not None:
                raise ValueError(
                    "a PENDING AccountExport carries no artifact, completion, expiry or failure")
        elif self.status is AccountExportStatus.READY:
            if not has_artifact or self.completed_at is None or self.expires_at is None:
                raise ValueError(
                    "a READY AccountExport must carry storage_key, byte_size, completed_at and "
                    "expires_at")
            if self.failure_reason is not None:
                raise ValueError("a READY AccountExport carries no failure_reason")
        elif self.status is AccountExportStatus.FAILED:
            if has_artifact or self.expires_at is not None:
                raise ValueError("a FAILED AccountExport leaves no artifact and no expiry")
            if self.failure_reason is None or self.completed_at is None:
                raise ValueError(
                    "a FAILED AccountExport must carry failure_reason and completed_at")
        else:  # AccountExportStatus.EXPIRED
            if has_artifact:
                raise ValueError(
                    "an EXPIRED AccountExport has had its artifact purged: storage_key and "
                    "byte_size are unset")
            if self.completed_at is None or self.expires_at is None:
                raise ValueError(
                    "an EXPIRED AccountExport keeps completed_at and expires_at as provenance")
            if self.failure_reason is not None:
                raise ValueError("an EXPIRED AccountExport carries no failure_reason")
        if self.expires_at is not None and self.completed_at is not None \
                and self.expires_at <= self.completed_at:
            raise ValueError("AccountExport expires_at must follow completed_at")
        if self.updated_at < self.created_at:
            raise ValueError("AccountExport updated_at must not precede created_at")
        return self

    def is_expired(self, as_of: datetime) -> bool:
        """Whether the archive's retention window has elapsed at `as_of`.

        A `PENDING` or `FAILED` export has no window and never expires by this measure. A `READY`
        one is expired once `as_of` reaches `expires_at`; the download check uses this so an
        archive stops being offered the instant its window passes, without waiting for the
        retention sweep to flip the status to `EXPIRED`.
        """
        return self.expires_at is not None and as_of >= self.expires_at

    def is_downloadable(self, as_of: datetime) -> bool:
        """Whether the archive may be handed to the user at `as_of` (§25).

        `True` only for a `READY` export whose window has not elapsed. Fail-safe: a `READY`
        export past its `expires_at` reads as not downloadable even before a retention job has
        marked it `EXPIRED`, so the model never offers bytes the retention policy has promised to
        remove.
        """
        return self.status is AccountExportStatus.READY \
            and self.storage_key is not None and not self.is_expired(as_of)

    def completed(self, *, storage_key: str, byte_size: int, expires_at: datetime,
                  as_of: datetime) -> "AccountExport":
        """The `READY` export this becomes once its archive is stored (§25).

        Records where the archive is (`storage_key`), how big it is (`byte_size`) and when it
        lapses (`expires_at`), stamping `completed_at`/`updated_at` at `as_of`. Returns a new
        instance the validator has checked; a `READY` export is downloadable by construction.
        """
        return AccountExport(
            id=self.id,
            user_id=self.user_id,
            status=AccountExportStatus.READY,
            schema_version=self.schema_version,
            storage_key=storage_key,
            byte_size=byte_size,
            completed_at=as_of,
            expires_at=expires_at,
            failure_reason=None,
            created_at=self.created_at,
            updated_at=as_of,
        )

    def failed(self, *, reason: str, as_of: datetime) -> "AccountExport":
        """The `FAILED` export this becomes when production could not finish.

        Records the machine `reason` and stamps `completed_at`/`updated_at` at `as_of`, leaving no
        artifact and no expiry — a failed export never presents a half-written archive.
        """
        return AccountExport(
            id=self.id,
            user_id=self.user_id,
            status=AccountExportStatus.FAILED,
            schema_version=self.schema_version,
            storage_key=None,
            byte_size=None,
            completed_at=as_of,
            expires_at=None,
            failure_reason=reason,
            created_at=self.created_at,
            updated_at=as_of,
        )

    def expired(self, *, as_of: datetime) -> "AccountExport":
        """The `EXPIRED` export this becomes when the retention sweep purges its archive (§30).

        Clears `storage_key`/`byte_size` — the bytes are gone — while keeping `completed_at` and
        `expires_at` as provenance, and stamps `updated_at` at `as_of`. Only a `READY` export
        expires; the service calls this after the store has deleted the artifact.
        """
        return AccountExport(
            id=self.id,
            user_id=self.user_id,
            status=AccountExportStatus.EXPIRED,
            schema_version=self.schema_version,
            storage_key=None,
            byte_size=None,
            completed_at=self.completed_at,
            expires_at=self.expires_at,
            failure_reason=None,
            created_at=self.created_at,
            updated_at=as_of,
        )
