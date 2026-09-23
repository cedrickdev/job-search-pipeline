"""How an application reaches an employer, and how far it may be trusted.

Three small vocabularies the Phase 12 engine turns on, kept in one module because
they answer the same question from three angles — *by what route* an application
would be submitted, *how much* automation that route can safely bear, and *why* a
route had to stop and ask a human.

None of these decides policy. `AutomationMode` (docs/… `ApplicationPolicy`) is the
user's standing instruction; `AdapterSafetyLevel` is a *ceiling* an adapter
declares on top of it, because a channel the platform cannot drive safely must be
able to lower autonomy below what a policy would otherwise allow, never raise it
(docs/APPLICATION_ENGINE.md §59). The one-directional rule is the whole point.
"""
from enum import StrEnum


class ApplicationChannel(StrEnum):
    """The route an application takes to an employer (§8-10).

    Named once so the registry dispatches on a typed member rather than a platform
    string scattered through business code (§8: no `if platform == "greenhouse"`).
    The channels are ordered loosely from most machine-friendly to least:

    - `ATS_API` — a first-class API the ATS publishes; the safest to automate;
    - `ATS_FORM` — a known ATS's web form (Greenhouse, Lever, Ashby, Umantis…);
    - `DIRECT_FORM` — an employer's own hosted application form;
    - `BROWSER` — a page only a browser can drive (LinkedIn, WTJ, Migros…);
    - `EMAIL` — an application sent to a stated recipient address (§62);
    - `MANUAL` — the platform prepares, the human submits; the honest default when
      nothing above fits;
    - `UNSUPPORTED` — the platform has no way to apply here at all, so it says so
      rather than pretending a `MANUAL` hand-off it cannot even set up.
    """

    ATS_API = "ATS_API"
    ATS_FORM = "ATS_FORM"
    DIRECT_FORM = "DIRECT_FORM"
    BROWSER = "BROWSER"
    EMAIL = "EMAIL"
    MANUAL = "MANUAL"
    UNSUPPORTED = "UNSUPPORTED"


class AdapterSafetyLevel(StrEnum):
    """How much autonomy a channel's adapter can safely bear (§59).

    A ceiling, not a mode. An adapter states the most autonomy it can be trusted
    with, and the execution gate takes the *minimum* of this and the user's policy:
    a `SUPPORTED_WITH_REVIEW` adapter under an `AUTOPILOT` policy still stops for a
    human, because the adapter — not the policy — knows the channel cannot be driven
    unattended without risking a wrong or duplicate submission. It can lower
    autonomy, never raise it.

    - `FULLY_SUPPORTED` — the platform can prepare *and* submit unattended when the
      policy also allows it;
    - `SUPPORTED_WITH_REVIEW` — it can prepare and fill, but a human must approve the
      actual submission whatever the policy says;
    - `MANUAL_ONLY` — it can only prepare materials; the human submits (the generic
      adapter's level, §13);
    - `UNSUPPORTED` — it cannot even prepare; there is nothing to automate.
    """

    FULLY_SUPPORTED = "FULLY_SUPPORTED"
    SUPPORTED_WITH_REVIEW = "SUPPORTED_WITH_REVIEW"
    MANUAL_ONLY = "MANUAL_ONLY"
    UNSUPPORTED = "UNSUPPORTED"

    @property
    def rank(self) -> int:
        """Position on the autonomy scale. Comparable; the string values are not."""
        return _SAFETY_RANK[self]

    @property
    def permits_unattended_submission(self) -> bool:
        """Whether this level, on its own, allows submitting without a human.

        Only `FULLY_SUPPORTED` does. The gate still requires the *policy* to permit
        it too — this is the adapter's half of an AND, never a grant on its own.
        """
        return self is AdapterSafetyLevel.FULLY_SUPPORTED

    @property
    def permits_preparation(self) -> bool:
        """Whether the platform may prepare materials on this channel at all."""
        return self is not AdapterSafetyLevel.UNSUPPORTED


_SAFETY_RANK: dict[AdapterSafetyLevel, int] = {
    AdapterSafetyLevel.UNSUPPORTED: 0,
    AdapterSafetyLevel.MANUAL_ONLY: 1,
    AdapterSafetyLevel.SUPPORTED_WITH_REVIEW: 2,
    AdapterSafetyLevel.FULLY_SUPPORTED: 3,
}


class HumanRequiredReason(StrEnum):
    """Why an application had to stop and ask a person (§19-27).

    Every member is a fact the platform *found* and refused to guess past, never a
    limitation it hid. The two at the top are the safety absolutes: the platform
    never solves a CAPTCHA and never completes an MFA challenge (§21), so meeting
    one is an immediate, typed hand-off — not a retry, not a bypass.

    - `CAPTCHA_PRESENT` — a human-verification challenge stands in the way;
    - `MFA_REQUIRED` — a second authentication factor is demanded;
    - `LOGIN_REQUIRED` — the channel needs credentials the platform will not supply
      unattended;
    - `UNKNOWN_REQUIRED_FIELD` — a required field the platform has no answer for
      (§25: an unknown required answer is never guessed as N/A, 0, Yes or No);
    - `SENSITIVE_QUESTION` — a legal, demographic or otherwise sensitive question
      (§26: a protected characteristic is never inferred);
    - `AMBIGUOUS_FORM` — the form's structure could not be understood well enough to
      fill safely;
    - `UPLOAD_UNRESOLVED` — a required document upload could not be satisfied;
    - `FORM_CHANGED` — the form changed between preparation and submission, so the
      prepared answers can no longer be trusted (§34);
    - `UNSUPPORTED_CHANNEL` — the channel has no automated path and needs a human to
      apply (the conservative generic outcome, §13).
    """

    CAPTCHA_PRESENT = "CAPTCHA_PRESENT"
    MFA_REQUIRED = "MFA_REQUIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    UNKNOWN_REQUIRED_FIELD = "UNKNOWN_REQUIRED_FIELD"
    SENSITIVE_QUESTION = "SENSITIVE_QUESTION"
    AMBIGUOUS_FORM = "AMBIGUOUS_FORM"
    UPLOAD_UNRESOLVED = "UPLOAD_UNRESOLVED"
    FORM_CHANGED = "FORM_CHANGED"
    UNSUPPORTED_CHANNEL = "UNSUPPORTED_CHANNEL"
