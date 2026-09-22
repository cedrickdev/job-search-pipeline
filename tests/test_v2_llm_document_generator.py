# tests/test_v2_llm_document_generator.py
"""The model-backed `LLMDocumentGenerator`, over a real router and a fake provider.

This is the Phase 11 wire into Phase 10's seam, so the tests hold the three boundaries
that keep the truth guarantee physical when a model — not a deterministic rule —
proposes the content:

- the model's answer is re-validated against the domain document (a non-JSON or
  malformed answer is a typed `STRUCTURED_OUTPUT_INVALID`, never a half-built document);
- the request the router runs carries the prompt's provenance and the candidate's own
  evidence ids, and nothing else can be cited;
- the Evidence Guard stays *outside* the generator — run through `DocumentService`, a
  model that invents a fact produces a REJECTED version, exactly as a deterministic
  fabrication would, because the guard is the trust boundary either way.

The provider is `tests/v2_llm.FakeProvider`, returning canned text over a real
`LLMRouter`/`LLMProviderRegistry`: the behaviour under test is the adapter's parsing,
routing and privacy, not a transport's.
"""
import json
from datetime import UTC, datetime

import pytest

from backend.app.documents import CandidateEvidenceGuard, LocalDocumentArtifactStore
from backend.app.documents.generator import InsufficientEvidence
from backend.app.documents.llm_generator import LLM_GENERATOR_KEY, LLMDocumentGenerator
from backend.app.domain.candidate import (
    CandidateClaim,
    CandidateEvidence,
    CandidateProfile,
    ClaimType,
    EvidenceKind,
    EvidenceProvenance,
)
from backend.app.domain.documents import (
    CandidateDocumentType,
    CoverLetterDocument,
    DocumentStatus,
    GenerationContext,
    ResumeDocument,
)
from backend.app.domain.identifiers import (
    default_candidate_profile_id,
    new_claim_id,
    new_evidence_id,
    new_user_id,
)
from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import LLMRouter, PrivacyClass, RoutingPolicy
from backend.app.services.documents import DocumentService
from tests.v2_builders import an_opportunity
from tests.v2_fakes import (
    FakeCandidateDocumentRepository,
    FakeCandidateProfileRepository,
    FakeLLMRunRepository,
    FakeOpportunityRepository,
)
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)

# The structured capability every document prompt requires (its render attaches a
# StructuredOutputSpec), so a provider that is to serve one must claim it.
_DOC_CAPS = frozenset({BASELINE_CAPABILITY, Capability.STRUCTURED_OUTPUT,
                       Capability.SYSTEM_INSTRUCTIONS})


def _profile(user_id=None):
    user_id = user_id if user_id is not None else new_user_id()
    summary = CandidateEvidence(
        id=new_evidence_id(), user_id=user_id, kind=EvidenceKind.CV_SUMMARY,
        provenance=EvidenceProvenance.BASE_CV,
        summary="Backend engineer with 8 years of experience", recorded_at=NOW)
    checkout = CandidateEvidence(
        id=new_evidence_id(), user_id=user_id, kind=EvidenceKind.CV_BULLET,
        provenance=EvidenceProvenance.BASE_CV, reference_key="acme-checkout",
        summary="Rebuilt the checkout flow, cutting latency 30%", recorded_at=NOW)
    experience = CandidateClaim(
        id=new_claim_id(), user_id=user_id, claim_type=ClaimType.EXPERIENCE,
        label="Software Engineer — Acme", detail="2021–2024, Lausanne",
        evidence_ids=(checkout.id,))
    skill = CandidateClaim(
        id=new_claim_id(), user_id=user_id, claim_type=ClaimType.SKILL,
        label="Python", evidence_ids=(summary.id,))
    return CandidateProfile(
        id=default_candidate_profile_id(user_id), user_id=user_id,
        display_name="Ada Lovelace", headline="Backend engineer",
        evidence=(summary, checkout), claims=(experience, skill), updated_at=NOW)


def _context(profile, opportunity, document_type=CandidateDocumentType.RESUME):
    return GenerationContext(
        document_type=document_type, target_language="en",
        candidate_profile_id=profile.id, opportunity_id=opportunity.id)


def _valid_resume_json(profile) -> str:
    """A résumé the model could truthfully return: cites only held evidence, own name."""
    summary = next(e for e in profile.evidence if e.kind is EvidenceKind.CV_SUMMARY)
    checkout = next(e for e in profile.evidence if e.reference_key == "acme-checkout")
    return json.dumps({
        "full_name": profile.display_name,
        "headline": profile.headline,
        "summary": {"text": summary.summary, "evidence_ids": [str(summary.id)]},
        "experience": [{
            "heading": "Software Engineer — Acme",
            "evidence_ids": [str(checkout.id)],
            "bullets": [{"text": checkout.summary, "evidence_ids": [str(checkout.id)]}],
        }],
        "skill_groups": [{"name": "Skills", "skills": ["Python"]}],
    })


def _valid_cover_letter_json(profile) -> str:
    checkout = next(e for e in profile.evidence if e.reference_key == "acme-checkout")
    return json.dumps({
        "recipient": "Acme — Senior Engineer",
        "greeting": "Dear Hiring Team,",
        "body": [{"text": checkout.summary, "evidence_ids": [str(checkout.id)]}],
        "closing": "Thank you.",
        "signature": profile.display_name,
    })


def _local_router(text: str, *, capabilities=_DOC_CAPS,
                  provider_key="local_llm") -> tuple[LLMRouter, FakeProvider]:
    provider = FakeProvider(provider_key=provider_key, local=True, text=text,
                            capabilities=capabilities)
    registry = LLMProviderRegistry()
    registry.register(provider)
    return LLMRouter(registry), provider


_LOCAL = RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY)


# --- the adapter parses a well-formed answer into a domain document ---------

async def test_a_valid_resume_answer_is_parsed_into_a_resume_document():
    profile = _profile()
    opportunity = an_opportunity()
    router, provider = _local_router(_valid_resume_json(profile))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    resume = await generator.generate_resume(
        profile=profile, opportunity=opportunity,
        context=_context(profile, opportunity))

    assert isinstance(resume, ResumeDocument)
    assert resume.full_name == profile.display_name
    assert resume.summary is not None
    assert provider.calls == 1  # it actually routed to the provider


async def test_a_valid_cover_letter_answer_is_parsed():
    profile = _profile()
    opportunity = an_opportunity()
    router, _ = _local_router(_valid_cover_letter_json(profile))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    letter = await generator.generate_cover_letter(
        profile=profile, opportunity=opportunity,
        context=_context(profile, opportunity,
                         document_type=CandidateDocumentType.COVER_LETTER))

    assert isinstance(letter, CoverLetterDocument)
    assert letter.signature == profile.display_name


async def test_the_generator_key_names_the_strategy_not_the_model():
    router, _ = _local_router(_valid_resume_json(_profile()))
    assert LLMDocumentGenerator(router=router, policy=_LOCAL).key == LLM_GENERATOR_KEY


# --- the model's output is untrusted: it is re-validated (§42) --------------

async def test_a_non_json_answer_is_a_typed_structured_output_error():
    profile = _profile()
    opportunity = an_opportunity()
    router, _ = _local_router("I'm sorry, I can't do that.")
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    with pytest.raises(LLMError) as caught:
        await generator.generate_resume(
            profile=profile, opportunity=opportunity,
            context=_context(profile, opportunity))
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_a_json_answer_missing_a_required_field_is_refused():
    profile = _profile()
    opportunity = an_opportunity()
    # Valid JSON, wrong shape for a ResumeDocument: no full_name.
    router, _ = _local_router(json.dumps({"headline": "x"}))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    with pytest.raises(LLMError) as caught:
        await generator.generate_resume(
            profile=profile, opportunity=opportunity,
            context=_context(profile, opportunity))
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_a_summary_line_that_cites_nothing_cannot_be_built():
    """`EvidenceBackedText` needs an id, so an uncited claim fails re-validation."""
    profile = _profile()
    opportunity = an_opportunity()
    bad = json.dumps({
        "full_name": profile.display_name,
        "summary": {"text": "A claim with no citation", "evidence_ids": []}})
    router, _ = _local_router(bad)
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    with pytest.raises(LLMError) as caught:
        await generator.generate_resume(
            profile=profile, opportunity=opportunity,
            context=_context(profile, opportunity))
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_the_validation_error_does_not_echo_the_offending_payload():
    """A ValidationError can quote the bad input; the typed error must not forward it."""
    profile = _profile()
    opportunity = an_opportunity()
    sensitive_shaped_value = "SSN-999-88-7777-should-not-appear"
    router, _ = _local_router(json.dumps({"headline": sensitive_shaped_value}))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    with pytest.raises(LLMError) as caught:
        await generator.generate_resume(
            profile=profile, opportunity=opportunity,
            context=_context(profile, opportunity))
    assert sensitive_shaped_value not in caught.value.detail


# --- the adapter refuses up front when there is nothing to build from -------

async def test_no_evidence_is_insufficient_evidence_before_any_call():
    user_id = new_user_id()
    barren = CandidateProfile(
        id=default_candidate_profile_id(user_id), user_id=user_id,
        display_name="Ada Lovelace", updated_at=NOW)
    opportunity = an_opportunity()
    router, provider = _local_router(_valid_resume_json(_profile()))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    with pytest.raises(InsufficientEvidence):
        await generator.generate_resume(
            profile=barren, opportunity=opportunity,
            context=_context(barren, opportunity))
    assert provider.calls == 0  # refused before reaching the provider


# --- the request carries provenance and only the candidate's evidence -------

async def test_the_routed_request_stamps_the_prompt_and_carries_the_evidence_ids():
    profile = _profile()
    opportunity = an_opportunity()
    router, provider = _local_router(_valid_resume_json(profile))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    await generator.generate_resume(
        profile=profile, opportunity=opportunity,
        context=_context(profile, opportunity))

    request = provider.seen[0]
    assert request.prompt_name == "resume_tailoring"
    assert request.prompt_version == "1.0"
    assert request.structured_output is not None
    # Every held evidence id is in the rendered payload — the only ids the model may
    # cite — and the posting is fenced as untrusted.
    payload = request.messages[0].content
    for item in profile.evidence:
        assert str(item.id) in payload
    assert "untrusted" in payload.lower()


# --- privacy is enforced by the router before any provider is reached -------

async def test_a_local_only_policy_never_reaches_a_remote_provider():
    """The prompt bears candidate evidence: LOCAL_ONLY must refuse a remote-only fleet."""
    profile = _profile()
    opportunity = an_opportunity()
    remote = FakeProvider(provider_key="remote_llm", local=False,
                          text=_valid_resume_json(profile), capabilities=_DOC_CAPS)
    registry = LLMProviderRegistry()
    registry.register(remote)
    generator = LLMDocumentGenerator(router=LLMRouter(registry), policy=_LOCAL)

    with pytest.raises(LLMError) as caught:
        await generator.generate_resume(
            profile=profile, opportunity=opportunity,
            context=_context(profile, opportunity))
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    assert remote.calls == 0


# --- telemetry: a generation is recorded, keyed to the prompt version -------

async def test_generation_through_the_recorder_writes_a_run():
    profile = _profile()
    opportunity = an_opportunity()
    router, _ = _local_router(_valid_resume_json(profile))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs)
    generator = LLMDocumentGenerator(
        router=router, policy=_LOCAL, recorder=recorder, user_id=profile.user_id)

    await generator.generate_resume(
        profile=profile, opportunity=opportunity,
        context=_context(profile, opportunity))

    assert len(runs.runs) == 1
    run = next(iter(runs.runs.values()))
    assert run.prompt_name == "resume_tailoring"
    assert run.prompt_version == "1.0"
    assert run.user_id == profile.user_id
    assert run.provider_key == "local_llm"


# --- the guard stays OUTSIDE the generator: a model that invents is REJECTED -

async def test_a_model_that_invents_a_fact_is_rejected_by_the_service_guard(tmp_path):
    """Run through DocumentService, an invented number is caught, stored, never rendered.

    The generator returns a résumé that cites a real evidence id but states a number
    that evidence never carried (50% where the record says 30%). The adapter's own
    re-validation accepts it — it is a structurally valid ResumeDocument — so the only
    thing standing between an invented fact and a rendered PDF is the guard the service
    runs *after* generation. This proves that boundary holds for a model-backed
    generator exactly as it does for a deterministic one.
    """
    profile = _profile()
    opportunity = an_opportunity()
    checkout = next(e for e in profile.evidence if e.reference_key == "acme-checkout")
    fabricated = json.dumps({
        "full_name": profile.display_name,
        "summary": {"text": "Cut latency by 50%", "evidence_ids": [str(checkout.id)]}})
    router, _ = _local_router(fabricated)
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    profiles = FakeCandidateProfileRepository()
    postings = FakeOpportunityRepository()
    documents = FakeCandidateDocumentRepository()
    await profiles.upsert(profile)
    await postings.upsert(opportunity)
    store = LocalDocumentArtifactStore(tmp_path / "artifacts")
    service = DocumentService(profiles, postings, documents, generator,
                              CandidateEvidenceGuard(), store)

    document = await service.generate(
        profile.user_id, opportunity.id, CandidateDocumentType.RESUME, now=NOW)

    version = document.latest
    assert version.status is DocumentStatus.REJECTED
    assert version.artifact is None
    assert version.generator_key == LLM_GENERATOR_KEY  # provenance recorded
    assert document.latest_usable() is None


async def test_a_truthful_model_answer_flows_all_the_way_to_a_rendered_pdf(tmp_path):
    """The happy path end to end: a truthful answer clears the guard and is rendered."""
    profile = _profile()
    opportunity = an_opportunity()
    router, _ = _local_router(_valid_resume_json(profile))
    generator = LLMDocumentGenerator(router=router, policy=_LOCAL)

    profiles = FakeCandidateProfileRepository()
    postings = FakeOpportunityRepository()
    documents = FakeCandidateDocumentRepository()
    await profiles.upsert(profile)
    await postings.upsert(opportunity)
    store = LocalDocumentArtifactStore(tmp_path / "artifacts")
    service = DocumentService(profiles, postings, documents, generator,
                              CandidateEvidenceGuard(), store)

    document = await service.generate(
        profile.user_id, opportunity.id, CandidateDocumentType.RESUME, now=NOW)

    assert document.latest.status is DocumentStatus.RENDERED
    stored = await service.download(profile.user_id, document.id)
    assert stored.content.startswith(b"%PDF")
