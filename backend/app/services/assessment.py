"""Assessment: running both engines over a pair and keeping the two verdicts.

This is the one application service Phase 9 adds, and it is deliberately thin. The
two engines in `backend.app.matching` and `backend.app.eligibility` hold every
decision; this service only decides *what to feed them* and *where to put what
they return*. Three rules from the phase order shape it:

**The two axes stay two records.** `evaluate` runs the match engine and the
eligibility engine independently and upserts each into its own repository. Neither
is a field of the other, a re-run of one never disturbs the other, and the match
score is never touched by the eligibility verdict — `Match: 92% / INELIGIBLE` and
`Match: 61% / ELIGIBLE` are both representable because the two numbers come from
two engines that never read each other.

**A match may be absent; an eligibility never is.** `evaluate_match` returns
`None` when no dimension can be scored (there is nothing to average, which is not
the same as a score of zero), so `match` is optional throughout. `evaluate_eligibility`
always emits at least the work-authorization gate, so an assessment always has a
verdict even when the honest answer to every gate is "we don't know yet".

**The owner comes from the session, never the body.** Every method takes `user_id`
and hands it to repositories whose signatures require it, and the pair is read
through the account's default profile — a request cannot ask about someone else's
candidate, and a stored verdict that is not this user's reads as absent.
"""
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.eligibility import EligibilityResult
from backend.app.domain.identifiers import OpportunityId, UserId
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity
from backend.app.eligibility import evaluate_eligibility
from backend.app.matching import evaluate_match
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CandidateProfileRepository,
    EligibilityResultRepository,
    MatchEvaluationRepository,
    OpportunityRepository,
)
from country_packs.contracts import CountryPack
from country_packs.registry import CountryPackRegistry


class AssessmentError(Exception):
    """Base class for the two preconditions an assessment can miss."""


class CandidateProfileNotFound(AssessmentError):
    """The account has not saved a candidate profile, so nothing can be assessed.

    Distinct from the opportunity being absent because the frontend acts on it
    differently — this one sends the user to onboarding — and it is the same code
    `GET /me/profile` answers with for the same state.
    """


class OpportunityNotFound(AssessmentError):
    """No posting is stored under that id.

    An `Opportunity` is a shared fact with no owner, so — unlike a user-owned
    row — "no such posting" is the only reason this can happen; there is no "not
    yours" to keep indistinguishable from it.
    """


@dataclass(frozen=True, slots=True)
class Assessment:
    """One opportunity, assessed on both axes for one candidate.

    Carries the `Opportunity` so a list can render a card without a second lookup,
    the `MatchEvaluation` (or `None` when nothing was scorable), and the
    `EligibilityResult` that is always present. The two verdicts sit side by side
    and neither derives from the other, which is the whole point of keeping them
    apart.
    """

    opportunity: Opportunity
    match: MatchEvaluation | None
    eligibility: EligibilityResult


class AssessmentService:
    """Run the match and eligibility engines over a pair, and store both verdicts.

    Every repository is handed in rather than reached for, so the request layer
    wires them (over PostgreSQL in production, over the fakes in a flow test) and
    this class never learns which. The country pack registry is read-only here: the
    service resolves the opportunity's country to a pack and passes it to the
    engines, which decide what it means.
    """

    def __init__(self, profiles: CandidateProfileRepository,
                 opportunities: OpportunityRepository,
                 matches: MatchEvaluationRepository,
                 eligibilities: EligibilityResultRepository,
                 packs: CountryPackRegistry) -> None:
        self._profiles = profiles
        self._opportunities = opportunities
        self._matches = matches
        self._eligibilities = eligibilities
        self._packs = packs

    async def evaluate(self, user_id: UserId, opportunity_id: OpportunityId, *,
                       now: datetime) -> Assessment:
        """Assess one posting for this account's profile, storing both verdicts.

        Idempotent by construction: both verdicts are keyed on
        `(candidate_profile_id, opportunity_id)`, so re-evaluating a pair replaces
        the previous answer rather than accumulating a row per run. The match is
        upserted only when the engine produced one — an unscorable pair leaves no
        match row and the assessment reports the absence, which the presentation
        layer renders as the UNKNOWN band rather than as a zero.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound(str(opportunity_id))

        pack = self._pack_for(opportunity)
        eligibility = await self._eligibilities.upsert(
            evaluate_eligibility(profile, opportunity, pack=pack, now=now))
        match = evaluate_match(profile, opportunity, pack=pack, now=now)
        stored_match = None if match is None else await self._matches.upsert(match)
        return Assessment(opportunity=opportunity, match=stored_match,
                          eligibility=eligibility)

    async def assessment_for(self, user_id: UserId,
                             opportunity_id: OpportunityId) -> Assessment | None:
        """The stored assessment for one pair, or `None` if it was never evaluated.

        A pure read: it never runs an engine and never writes. `None` covers three
        cases that must stay indistinguishable to the caller — the account has no
        profile, the pair has not been evaluated, or the posting belongs to a run
        this user never made — so a 404 for one cannot be told apart from a 404 for
        another, and another user's verdict cannot be probed by id.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            return None
        eligibility = await self._eligibilities.get_for_pair(
            user_id, profile.id, opportunity_id)
        if eligibility is None:
            return None
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            # The FK makes this unreachable in practice; treated as "no assessment"
            # rather than trusted away, so a future schema change cannot turn it
            # into an AttributeError on the response.
            return None
        match = await self._matches.get_for_pair(user_id, profile.id, opportunity_id)
        return Assessment(opportunity=opportunity, match=match,
                          eligibility=eligibility)

    async def list_assessments(self, user_id: UserId, *,
                               limit: int = DEFAULT_LIMIT) -> tuple[Assessment, ...]:
        """This user's assessed pairs, most recently determined first.

        Driven by the eligibility verdicts because they are the record that always
        exists after an evaluation — a pair may have no match (nothing scorable)
        but never has no eligibility. Both axes are returned untouched: the order
        is chronological, not a ranking, so an INELIGIBLE pair is never pushed down
        by pretending its match score is low (docs/MATCHING_ELIGIBILITY.md §Ranking).
        """
        verdicts = await self._eligibilities.list_for_user(user_id, limit=limit)
        assessments: list[Assessment] = []
        for eligibility in verdicts:
            opportunity = await self._opportunities.get(eligibility.opportunity_id)
            if opportunity is None:
                continue
            match = await self._matches.get_for_pair(
                user_id, eligibility.candidate_profile_id,
                eligibility.opportunity_id)
            assessments.append(Assessment(opportunity=opportunity, match=match,
                                          eligibility=eligibility))
        return tuple(assessments)

    def _pack_for(self, opportunity: Opportunity) -> CountryPack | None:
        """The country pack for the posting's country, or `None` if there is none.

        `find` rather than `get`: a posting in a country with no pack is assessed
        without one — the engines fall back to neutral defaults — rather than
        failing the whole request. A posting that names no country has no pack
        either, and the engines route the missing country to INCOMPLETE.
        """
        country = opportunity.location.country if opportunity.location else None
        if country is None:
            return None
        return self._packs.find(country)
