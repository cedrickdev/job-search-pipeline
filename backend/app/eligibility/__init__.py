"""The deterministic eligibility engine and its policy.

`evaluate_eligibility` is the entry point; `policy` carries the version stamp and
the legal-safety rule that keeps unverified pack data from ever refusing an
application. Import from here so a later reshuffle stays internal.
"""
from backend.app.eligibility.engine import evaluate_eligibility
from backend.app.eligibility.policy import (
    ELIGIBILITY_POLICY_VERSION,
    permitted_block_status,
)

__all__ = [
    "ELIGIBILITY_POLICY_VERSION",
    "evaluate_eligibility",
    "permitted_block_status",
]
