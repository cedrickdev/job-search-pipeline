"""The one typed refusal the Career Intelligence Loop raises — `CareerError` (§54, §90).

Every service in this package refuses the same way the interview service does: a single
exception carrying a stable `CareerErrorCode` and a secret-free `detail`, mapped to an HTTP
status once in `backend.app.api.errors` rather than in each route. One exception with a closed
code vocabulary — not a class per condition — because the code is what a client branches on and
the sentence is only for a human reading a log; keeping them together means the status for
"that proposal is not yours" is decided in exactly one table.

Two disciplines the codes encode, both acceptance-critical:

- **A user-owned resource that is absent or foreign reads as one thing.** `APPLICATION_NOT_
  FOUND`, `OUTCOME_NOT_FOUND` and `PROPOSAL_NOT_FOUND` do not tell "no such row" apart from
  "not yours", so a caller cannot enumerate another account's ids by probing
  (docs/ENGINEERING_STANDARDS.md §Security). The load *is* the authorization check.
- **Loosening the application policy is never silent.** `SENSITIVE_CONFIRMATION_REQUIRED` is
  the refusal a proposal that widens what the platform may apply to earns until the user
  confirms a second time — the acceptance rule "the system never silently expands the user's
  application policy" turned into a status a surface must handle.

`detail` is composed from ids, states and fixed sentences the domain owns; it never carries a
provider's message, a candidate's words or a payload, so surfacing it echoes nothing sensitive.
"""
from enum import StrEnum


class CareerErrorCode(StrEnum):
    """The closed vocabulary of ways a career-loop service refuses (§54, §90).

    Grouped by the service that raises it, and each maps to exactly one HTTP status in
    `backend.app.api.errors._CAREER_STATUS`. A member a provider or a client cannot invent —
    it is an enum, not a free string — so a new refusal is a member added here plus a row in
    that table, never an ad-hoc code at a call site.

    - `APPLICATION_NOT_FOUND` — the application an outcome names is not this account's (404);
    - `OUTCOME_NOT_FOUND` — the outcome to correct or retract is not this account's (404);
    - `OUTCOME_NOT_EFFECTIVE` — the outcome is already superseded or retracted, so it cannot be
      corrected or retracted again (409);
    - `OPPORTUNITY_NOT_FOUND` — the opportunity to classify does not exist (404; a posting is a
      shared fact with no owner, so there is no "not yours" to fold in);
    - `PROPOSAL_NOT_FOUND` — the strategy proposal is not this account's (404);
    - `PROPOSAL_NOT_OPEN` — the proposal is no longer `PROPOSED`, and nothing was executed for
      it, so a stale confirm or dismiss cannot act (409);
    - `PROPOSAL_EXPIRED` — the proposal lapsed past its `expires_at` before anyone approved it
      (409); approval marks it `EXPIRED` and refuses;
    - `STRATEGY_PROPOSAL_STALE` — the live target moved on since the proposal was drafted, so
      applying its before/after would clobber newer state (409); the version precondition;
    - `STRATEGY_TARGET_NOT_FOUND` — the search or policy the proposal edits no longer exists at
      approval time (409, a conflict with current state rather than a bad request);
    - `SENSITIVE_CONFIRMATION_REQUIRED` — the change loosens a safety brake and needs an
      explicit second confirmation before it may run (409).
    """

    APPLICATION_NOT_FOUND = "APPLICATION_NOT_FOUND"
    OUTCOME_NOT_FOUND = "OUTCOME_NOT_FOUND"
    OUTCOME_NOT_EFFECTIVE = "OUTCOME_NOT_EFFECTIVE"
    OPPORTUNITY_NOT_FOUND = "OPPORTUNITY_NOT_FOUND"
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    PROPOSAL_NOT_OPEN = "PROPOSAL_NOT_OPEN"
    PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
    STRATEGY_PROPOSAL_STALE = "STRATEGY_PROPOSAL_STALE"
    STRATEGY_TARGET_NOT_FOUND = "STRATEGY_TARGET_NOT_FOUND"
    SENSITIVE_CONFIRMATION_REQUIRED = "SENSITIVE_CONFIRMATION_REQUIRED"


class CareerError(Exception):
    """A typed, secret-free refusal from a career-loop service (§54, §90).

    Carries the stable `code` a client branches on and a `detail` sentence for a human. The
    detail is composed by the service from the domain's own vocabulary — ids, states, fixed
    phrases — never a provider message or user input, so `backend.app.api.errors` can surface
    it verbatim without leaking anything. The API decides the status from `code` alone.
    """

    def __init__(self, code: CareerErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
