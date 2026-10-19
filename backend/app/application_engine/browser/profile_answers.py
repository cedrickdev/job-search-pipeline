"""Proposing answers from the candidate profile — trusted facts, never inventions (§9, §12).

A driver reads the *questions* a form asks; this turns the profile facts the platform actually
holds into `ApplicationAnswerProposal`s for the standard fields it recognizes, with
`CANDIDATE_PROFILE` provenance so `resolve_answer` trusts them (they are copied facts, not guesses).
It proposes *only* what the profile genuinely carries:

- the display name, split into first/last or offered whole, for name fields;
- the base location, for a location field.

Contact details are proposed only when the candidate explicitly stored them in their own profile.
Missing contact data still produces no proposal, so a mandatory field becomes a human stop rather
than an invented answer.
"""
from __future__ import annotations

from backend.app.application_engine.browser import semantics
from backend.app.domain.application_answer import (
    AnswerProvenance,
    ApplicationAnswerProposal,
    ApplicationQuestion,
)
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import Location


def _format_location(location: Location | None) -> str | None:
    """A human-readable "City, Country" from a `Location`, or `None` if it says nothing useful."""
    if location is None:
        return None
    if location.raw:
        return location.raw
    parts = [part for part in (location.city, location.region, location.country) if part]
    return ", ".join(parts) if parts else None


def _proposed_value(key: str, profile: CandidateProfile) -> str | None:
    """The profile value for a canonical field key, or `None` when the profile does not hold it."""
    name_parts = profile.display_name.split()
    if key == semantics.FIRST_NAME:
        return name_parts[0] if name_parts else None
    if key == semantics.LAST_NAME:
        return " ".join(name_parts[1:]) if len(name_parts) > 1 else None
    if key == semantics.FULL_NAME:
        return profile.display_name
    if key == semantics.LOCATION:
        return _format_location(profile.base_location)
    if key == semantics.EMAIL:
        return str(profile.contact.email) if profile.contact is not None else None
    if key == semantics.PHONE:
        return profile.contact.phone if profile.contact is not None else None
    # LINKEDIN and every custom field: the profile holds no trustworthy value, so nothing is
    # proposed and a required field stops for a human (§11-12).
    return None


def propose_from_profile(
        questions: tuple[ApplicationQuestion, ...],
        profile: CandidateProfile) -> tuple[ApplicationAnswerProposal, ...]:
    """Proposals for the standard fields the profile can satisfy, and nothing else (§9, §12).

    One proposal per question whose canonical key the profile holds a real value for, stamped
    `CANDIDATE_PROFILE` so `resolve_answer` fills it without a human. A sensitive question is never
    proposed for — `resolve_answer` will route it to a human regardless — and a custom or
    contact-detail field gets no proposal, so a required one becomes `UNKNOWN_REQUIRED_FIELD`.
    """
    proposals: list[ApplicationAnswerProposal] = []
    for question in questions:
        if question.sensitivity.needs_human:
            continue
        value = _proposed_value(question.key, profile)
        if value:
            proposals.append(ApplicationAnswerProposal(
                question_key=question.key,
                value=value,
                provenance=AnswerProvenance.CANDIDATE_PROFILE))
    return tuple(proposals)
