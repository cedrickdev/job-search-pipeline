"""The "recommend" link — a deterministic engine that reads metrics and cites them (§26-33).

Third link of the spine `observe → measure → recommend → user approves → existing service
executes`, and the one the acceptance rule "recommendations cite internal evidence/metrics"
lands on. The engine is deterministic and reads a *typed `CareerAnalytics` report*, never a
database cursor: it compares the funnel's own slices and, when one converts notably better or
worse than another on a base of real applications, it emits a typed `CareerRecommendation`
whose evidence is exactly the rates it compared — numerators, denominators and sample sizes a
reader can trace back to the report and its `analytics_version`.

Three disciplines it keeps, each mirroring a rule the domain already enforces:

- **Evidence or nothing, never from weak data (§62).** A slice earns a citation only when its
  base clears `MIN_RECOMMENDATION_SAMPLE_SIZE`; a comparison needs two such slices with a gap
  of at least `NOTABLE_RATE_GAP`, so noise never becomes a suggestion. The domain model rejects
  thin evidence too — this is the engine refusing to build it in the first place.
- **No causal language (§62).** The deterministic summaries state the observed rates side by
  side ("A: 8/20, B: 2/20") and never say one *causes* the other; when a narrator polishes the
  wording, `asserts_causation` guards the result and a causal rewrite is dropped for the
  deterministic sentence.
- **Deterministic core, optional wording (§33).** The engine authors the recommendation and
  its evidence; a `RecommendationNarrator` may only rewrite `summary`/`detail`. It is optional,
  guarded, and falls back to the deterministic prose on any refusal — so provenance is
  `generator_key=None` for the template and the narrator's key only when its prose was kept.
"""
from dataclasses import dataclass
from datetime import datetime

from backend.app.career.analytics import CareerAnalyticsService
from backend.app.domain.analytics import (
    CareerAnalytics,
    DimensionBreakdown,
    DimensionCell,
    DimensionKind,
    RateKind,
)
from backend.app.domain.identifiers import (
    LLMRunId,
    UserId,
    career_recommendation_evidence_id,
    new_career_recommendation_id,
)
from backend.app.domain.recommendation import (
    MIN_RECOMMENDATION_SAMPLE_SIZE,
    CareerRecommendation,
    RecommendationEvidence,
    RecommendationKind,
    asserts_causation,
)
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CareerRecommendationRepository,
)

# The smallest gap between two slices' rates that is worth a suggestion (§62). Below this the
# difference is within the noise two small samples always carry, so the engine stays silent
# rather than reading a coin-flip as a signal. Ten points is deliberately coarse: a
# recommendation is a nudge a human then turns into a change, not a fine-grained optimizer.
NOTABLE_RATE_GAP = 0.10


@dataclass(frozen=True)
class _EvidenceSpec:
    """One cited metric before it is bound to a recommendation id — an id-free evidence row.

    The engine compares slices, then mints the recommendation id, then materializes these into
    `RecommendationEvidence` with the derived `(recommendation_id, ordinal)` id — so the draft
    stays a pure value the derivation can build without knowing the id yet.
    """

    dimension: DimensionKind
    dimension_key: str
    rate_kind: RateKind
    numerator: int
    denominator: int
    detail: str


@dataclass(frozen=True)
class RecommendationDraft:
    """A recommendation the deterministic core has reasoned out, before persistence or wording.

    Carries the `kind`, the deterministic `summary`/`detail` prose, and the evidence specs it
    rests on. A narrator may rewrite the prose; nothing may touch the evidence, which is the
    engine's own arithmetic.
    """

    kind: RecommendationKind
    summary: str
    detail: str | None
    evidence: tuple[_EvidenceSpec, ...]


@dataclass(frozen=True)
class NarratedProse:
    """Polished wording a narrator returned, paired with the run that produced it (§33).

    `summary`/`detail` replace the deterministic prose only after the wording guard clears them;
    `llm_run_id` is the run the recorder wrote, stamped onto the recommendation for provenance.
    """

    summary: str
    detail: str | None = None
    llm_run_id: LLMRunId | None = None


class RecommendationNarrator:
    """The seam a provider-backed wording polisher implements — optional, never authoritative.

    A narrator may only rephrase a draft's `summary`/`detail`; it is handed the deterministic
    draft and returns `NarratedProse`, and the engine re-checks the result before trusting it.
    The base is a no-op returning `None` (keep the deterministic prose), so the engine's default
    is fully deterministic and a real narrator overrides `phrase`.
    """

    key: str = "llm-worded/1"

    async def phrase(self, *, user_id: UserId,
                     draft: RecommendationDraft) -> NarratedProse | None:
        """Rephrase a draft, or return `None` to keep the deterministic wording."""
        return None


class CareerRecommendationEngine:
    """Turn one account's funnel report into evidence-backed suggestions with no authority (§26-33).

    Holds the analytics service it reads the typed report from, the write-once recommendation
    store, and an optional narrator. No clock in the constructor — `recommend` takes `now` — the
    convention every V2 service keeps. The engine never mutates anything but its own store: a
    recommendation cannot change a search, a policy or an application.
    """

    def __init__(self, analytics: CareerAnalyticsService,
                 recommendations: CareerRecommendationRepository, *,
                 narrator: RecommendationNarrator | None = None) -> None:
        self._analytics = analytics
        self._recommendations = recommendations
        self._narrator = narrator

    async def recommend(self, user_id: UserId, *, now: datetime,
                        horizon_days: int | None = None) -> tuple[CareerRecommendation, ...]:
        """Compute the report, derive suggestions from it, persist and return them (§26-33).

        The report is the typed `CareerAnalytics` the engine reasons over — never raw rows — so
        every recommendation cites a metric stamped with the `analytics_version` that produced
        it and pins the window/horizon snapshot it was drawn against. Each draft is materialized
        into a `CareerRecommendation` and persisted through a fingerprint gate: a suggestion
        whose logical content already exists for this account is returned unchanged rather than
        added again, so re-running over unchanged analytics is idempotent instead of a flood of
        duplicate rows. A draft resting on genuinely new evidence is a fresh observation, added.
        """
        report = (await self._analytics.report(user_id, now=now)
                  if horizon_days is None
                  else await self._analytics.report(user_id, now=now,
                                                    horizon_days=horizon_days))
        stored: list[CareerRecommendation] = []
        for draft in self._derive(report):
            stored.append(await self._persist(user_id, draft, report, now=now))
        return tuple(stored)

    async def latest(self, user_id: UserId, *,
                     limit: int = DEFAULT_LIMIT) -> tuple[CareerRecommendation, ...]:
        """This account's recommendations, most recently created first — a surface to review."""
        return await self._recommendations.list_for_user(user_id, limit=limit)

    # --- the deterministic derivation --------------------------------------

    def _derive(self, report: CareerAnalytics) -> tuple[RecommendationDraft, ...]:
        """Every suggestion the report supports, in a stable order (§26-31).

        Role family and source each yield a `PRIORITIZE`/`DEPRIORITIZE` pair when one slice
        notably out- or under-performs another on a real base; opportunity type yields a
        directionless `REVIEW` of the mix. The order is fixed (role, source, type) so the same
        report always produces the same sequence — a deterministic core a test can pin.
        """
        drafts: list[RecommendationDraft] = []
        drafts += self._extreme_pair(
            report.breakdown_for(DimensionKind.ROLE_FAMILY),
            prioritize=RecommendationKind.PRIORITIZE_ROLE_FAMILY,
            deprioritize=RecommendationKind.DEPRIORITIZE_ROLE_FAMILY)
        drafts += self._extreme_pair(
            report.breakdown_for(DimensionKind.SOURCE),
            prioritize=RecommendationKind.PRIORITIZE_SOURCE,
            deprioritize=RecommendationKind.DEPRIORITIZE_SOURCE)
        drafts += self._type_mix(report.breakdown_for(DimensionKind.OPPORTUNITY_TYPE))
        return tuple(drafts)

    def _extreme_pair(self, breakdown: DimensionBreakdown | None, *,
                      prioritize: RecommendationKind,
                      deprioritize: RecommendationKind) -> list[RecommendationDraft]:
        """The prioritize/deprioritize pair a dimension's best and worst slice earn, if any."""
        ranked = _rank_by_response(breakdown)
        if ranked is None:
            return []
        best, worst = ranked
        return [
            self._focus_draft(prioritize, focus=best, foil=worst),
            self._focus_draft(deprioritize, focus=worst, foil=best),
        ]

    def _type_mix(self, breakdown: DimensionBreakdown | None) -> list[RecommendationDraft]:
        """A single directionless `REVIEW_OPPORTUNITY_TYPE_MIX` when the types differ notably."""
        ranked = _rank_by_response(breakdown)
        if ranked is None:
            return []
        best, worst = ranked
        summary = (f"Vos taux de réponse varient selon le type de poste : "
                   f"{_pct(best)} pour {best.key}, {_pct(worst)} pour {worst.key}.")
        return [RecommendationDraft(
            kind=RecommendationKind.REVIEW_OPPORTUNITY_TYPE_MIX,
            summary=summary, detail=None,
            evidence=(_spec(DimensionKind.OPPORTUNITY_TYPE, best),
                      _spec(DimensionKind.OPPORTUNITY_TYPE, worst)))]

    def _focus_draft(self, kind: RecommendationKind, *,
                     focus: DimensionCell, foil: DimensionCell) -> RecommendationDraft:
        """A prioritize/deprioritize draft naming `focus`, with `foil` as the compared baseline.

        The prose states both observed rates side by side and never claims one causes the other;
        the evidence leads with `focus` (ordinal 0, the slice the suggestion is about) and cites
        `foil` (ordinal 1, the slice it is measured against), so both citations clear the sample
        minimum by construction — `_rank_by_response` only returns slices that already do.
        """
        verb = ("privilégier" if kind.value.startswith("PRIORITIZE") else "réduire")
        summary = (f"À {verb} : {focus.key} obtient {_pct(focus)} de réponses "
                   f"({_counts(focus)}), contre {_pct(foil)} pour {foil.key} "
                   f"({_counts(foil)}).")
        dimension = focus.dimension
        return RecommendationDraft(
            kind=kind, summary=summary, detail=None,
            evidence=(_spec(dimension, focus), _spec(dimension, foil)))

    # --- materialization and the guarded narrator seam ---------------------

    async def _persist(self, user_id: UserId, draft: RecommendationDraft,
                       report: CareerAnalytics, *, now: datetime) -> CareerRecommendation:
        """Materialize the deterministic recommendation, gate on its fingerprint, add it once.

        Builds the recommendation with its deterministic prose first — enough to compute the
        fingerprint, which is a pure function of the logical evidence and the snapshot window, not
        of the random id or the wording — then asks the store whether an identical logical
        recommendation already exists for this account. If one does, it is returned unchanged and
        nothing is written: regeneration over unchanged analytics is idempotent, and the narrator
        is never even called. Otherwise the wording is optionally polished and the fresh
        recommendation is added.
        """
        deterministic = self._build(user_id, draft, report, now=now)
        existing = await self._recommendations.find_by_fingerprint(
            user_id, deterministic.fingerprint)
        if existing is not None:
            return existing
        recommendation = await self._apply_prose(user_id, draft, deterministic)
        return await self._recommendations.add(recommendation)

    def _build(self, user_id: UserId, draft: RecommendationDraft,
               report: CareerAnalytics, *, now: datetime) -> CareerRecommendation:
        """The persistable recommendation with deterministic prose and the analytics snapshot.

        The id is minted here (random — a recommendation is a fresh observation), the evidence is
        bound to it by the derived `(id, ordinal)` key, and the prose is the deterministic
        draft's. `analytics_version`, `analytics_computed_at`, the `observation_horizon_days` in
        force and the observation `window_start`/`window_end` are copied from the report, so the
        recommendation is pinned to the exact snapshot it was drawn from — not merely the recipe
        version — and its `fingerprint` is stable across runs over that same snapshot.
        """
        recommendation_id = new_career_recommendation_id()
        evidence = tuple(
            RecommendationEvidence(
                id=career_recommendation_evidence_id(recommendation_id, ordinal),
                recommendation_id=recommendation_id, ordinal=ordinal,
                dimension=spec.dimension, dimension_key=spec.dimension_key,
                rate_kind=spec.rate_kind, numerator=spec.numerator,
                denominator=spec.denominator, sample_size=spec.denominator,
                detail=spec.detail)
            for ordinal, spec in enumerate(draft.evidence))
        return CareerRecommendation(
            id=recommendation_id, user_id=user_id, kind=draft.kind,
            analytics_version=report.analytics_version,
            analytics_computed_at=report.computed_at,
            observation_horizon_days=report.funnel.censoring.observation_horizon_days,
            window_start=report.window.earliest_applied_at,
            window_end=report.window.latest_applied_at,
            summary=draft.summary, detail=draft.detail, evidence=evidence,
            generator_key=None, llm_run_id=None, created_at=now)

    async def _apply_prose(self, user_id: UserId, draft: RecommendationDraft,
                           deterministic: CareerRecommendation) -> CareerRecommendation:
        """The recommendation to persist — the deterministic one, or a narrated copy if kept.

        Runs the guarded narrator over the draft; when it returns nothing trustworthy the
        deterministic recommendation stands unchanged (`generator_key is None`). When a narrator's
        wording clears the guard, only `summary`/`detail` and the provenance are copied over — the
        id, evidence and analytics snapshot are the engine's own and are never touched.
        """
        summary, detail, generator_key, llm_run_id = await self._prose(user_id, draft)
        if generator_key is None:
            return deterministic
        return deterministic.model_copy(update={
            "summary": summary, "detail": detail,
            "generator_key": generator_key, "llm_run_id": llm_run_id})

    async def _prose(self, user_id: UserId, draft: RecommendationDraft
                     ) -> tuple[str, str | None, str | None, LLMRunId | None]:
        """The wording to persist and its provenance — deterministic unless a narrator's is kept.

        With no narrator, the deterministic prose stands and provenance is `None`. A narrator's
        rewrite is trusted only when it is present, non-empty and free of causal language
        (`asserts_causation`); anything else — a refusal, an empty rewrite, a causal claim, or a
        raised error — falls back to the deterministic sentence, so a provider can never weaken
        the "no causal language" rule or blank out a recommendation.
        """
        if self._narrator is None:
            return draft.summary, draft.detail, None, None
        try:
            prose = await self._narrator.phrase(user_id=user_id, draft=draft)
        except Exception:  # a narrator failure is never fatal to a deterministic recommendation
            prose = None
        if prose is None or not prose.summary.strip():
            return draft.summary, draft.detail, None, None
        if asserts_causation(prose.summary) or (
                prose.detail is not None and asserts_causation(prose.detail)):
            return draft.summary, draft.detail, None, None
        return prose.summary, prose.detail, self._narrator.key, prose.llm_run_id


def _rank_by_response(breakdown: DimensionBreakdown | None
                      ) -> tuple[DimensionCell, DimensionCell] | None:
    """The best and worst keyed slice by RESPONSE rate, or `None` when no comparison is sound.

    A comparison is sound only with at least two *keyed* slices (the `None` unclassified cell is
    never a recommendation target) whose RESPONSE base each clears `MIN_RECOMMENDATION_SAMPLE_
    SIZE`, and whose best and worst rates differ by at least `NOTABLE_RATE_GAP`. The sort is
    rate-descending with the key as a stable tie-break, so the same report always names the same
    best and worst — the determinism the engine rests on.
    """
    if breakdown is None:
        return None
    qualifying = []
    for cell in breakdown.cells:
        if cell.key is None:
            continue
        rate = cell.rate_for(RateKind.RESPONSE)
        if rate is None or rate.rate is None:
            continue
        if rate.denominator < MIN_RECOMMENDATION_SAMPLE_SIZE:
            continue
        qualifying.append(cell)
    if len(qualifying) < 2:
        return None
    qualifying.sort(key=lambda cell: cell.key or "")
    qualifying.sort(key=lambda cell: _response_rate(cell), reverse=True)
    best, worst = qualifying[0], qualifying[-1]
    if _response_rate(best) - _response_rate(worst) < NOTABLE_RATE_GAP:
        return None
    return best, worst


def _response_rate(cell: DimensionCell) -> float:
    """The slice's RESPONSE rate as a float — only called on cells `_rank_by_response` kept."""
    rate = cell.rate_for(RateKind.RESPONSE)
    assert rate is not None and rate.rate is not None
    return rate.rate


def _spec(dimension: DimensionKind, cell: DimensionCell) -> _EvidenceSpec:
    """The `_EvidenceSpec` citing one slice's RESPONSE rate — its numbers, not a judgement."""
    rate = cell.rate_for(RateKind.RESPONSE)
    assert rate is not None
    return _EvidenceSpec(
        dimension=dimension, dimension_key=cell.key or "",
        rate_kind=RateKind.RESPONSE, numerator=rate.numerator,
        denominator=rate.denominator,
        detail=f"{cell.key} : {rate.numerator} réponses sur {rate.denominator} candidatures.")


def _pct(cell: DimensionCell) -> str:
    """A slice's RESPONSE rate as a rounded percentage for prose (e.g. "30 %")."""
    return f"{round(_response_rate(cell) * 100)} %"


def _counts(cell: DimensionCell) -> str:
    """A slice's RESPONSE numerator/denominator for prose (e.g. "6/20")."""
    rate = cell.rate_for(RateKind.RESPONSE)
    assert rate is not None
    return f"{rate.numerator}/{rate.denominator}"
