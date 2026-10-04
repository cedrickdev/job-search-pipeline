"""Add typed fail-closed reasons for reserved browser submissions.

Revision ID: 0021
Revises: 0020
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_submission_attempts_submission_attempt_human_reason"
_ORIGINAL = (
    "CAPTCHA_PRESENT", "MFA_REQUIRED", "LOGIN_REQUIRED", "UNKNOWN_REQUIRED_FIELD",
    "SENSITIVE_QUESTION", "AMBIGUOUS_FORM", "UPLOAD_UNRESOLVED", "FORM_CHANGED",
    "UNSUPPORTED_CHANNEL",
)


def _replace_allowed(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "submission_attempts", type_="check")
    allowed = ", ".join("'" + value + "'" for value in values)
    op.create_check_constraint(
        op.f(_CONSTRAINT), "submission_attempts",
        sa.text(f"human_required_reason IN ({allowed})"))


def upgrade() -> None:
    _replace_allowed((*_ORIGINAL, "POLICY_CHANGED", "AUTOMATION_DISABLED"))


def downgrade() -> None:
    # A downgrade may only remove values after operators have resolved such rows.
    _replace_allowed(_ORIGINAL)
