"""Retention — sweeping legitimately-temporary data on a schedule (Phase 16 §30-31).

The application-layer package that turns the retention *primitives* on the repositories and the
retention *policy* in `RetentionSettings` into one idempotent, operator-scope sweep. It owns no
clock and no owner: `now` is passed in, and every category it touches is temporary by the policy's
own definition, so the sweep can never reach candidate evidence, application audit history,
outcomes or user-created documents (§30). See `docs/DATA_LIFECYCLE.md` for the operator-facing
description.
"""
from backend.app.retention.service import (
    RetentionPurgeFailure,
    RetentionService,
    RetentionSweepReport,
    format_retention_report,
)

__all__ = [
    "RetentionPurgeFailure",
    "RetentionService",
    "RetentionSweepReport",
    "format_retention_report",
]
