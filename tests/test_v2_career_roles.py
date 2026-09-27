# tests/test_v2_career_roles.py
"""`RoleClassificationService` : une supposition déterministe qu'un humain peut toujours corriger.

C'est le maillon qui donne à l'analytique son axe « par famille de rôle » sans jamais inventer de
classifieur ni appeler un fournisseur (§18, §47). Deux disciplines portent ce module :

- **le déterministe ne connaît que le titre** : `classify` lit le titre du poste via la règle pure
  `classify_role_family`, et un titre qu'aucune règle ne place reste *non classé* (`None`) — jamais
  un fourre-tout « Autre » qui ferait mentir les chiffres par famille ;
- **le manuel prime, et survit** : `set_manual` nomme une famille (le domaine refuse une
  classification manuelle qui n'en nomme pas) et le backfill `classify` ne l'écrase jamais.

L'opportunité est un fait partagé — lue par id, sans propriétaire — mais la classification est
possédée : chaque compte ne voit que la sienne. Tout tourne sur `tests/v2_fakes.py`.
"""
from datetime import UTC, datetime

import pytest

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.career.roles import RoleClassificationService
from backend.app.domain.identifiers import role_classification_id
from backend.app.domain.role import RoleFamily, RoleFamilyProvenance
from tests.v2_builders import (
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    OTHER_USER,
    USER,
    a_source_record,
    an_opportunity,
)
from tests.v2_fakes import FakeOpportunityRepository, FakeRoleClassificationRepository

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 5, 3, 9, 0, tzinfo=UTC)


def build_service() -> tuple[RoleClassificationService, FakeRoleClassificationRepository,
                             FakeOpportunityRepository]:
    roles = FakeRoleClassificationRepository()
    opportunities = FakeOpportunityRepository()
    return RoleClassificationService(roles, opportunities), roles, opportunities


async def _seed_opportunity(opportunities: FakeOpportunityRepository, *,
                            opportunity_id=OPPORTUNITY, title: str,
                            external_id: str = "posting-1",
                            fingerprint: str = "fingerprint-1"):
    return await opportunities.upsert(an_opportunity(
        id=opportunity_id, title=title,
        source=a_source_record(external_id=external_id),
        dedup_fingerprint=fingerprint))


# --- the deterministic classification ---------------------------------------------------


async def test_classify_assigns_the_family_the_title_keyword_rule_finds() -> None:
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Senior Software Engineer")

    classification = await service.classify(USER, OPPORTUNITY, now=T0)

    assert classification.role_family is RoleFamily.SOFTWARE_ENGINEERING
    assert classification.provenance is RoleFamilyProvenance.DETERMINISTIC_TITLE
    assert classification.id == role_classification_id(USER, OPPORTUNITY)
    assert classification.created_at == T0
    assert classification.updated_at == T0


async def test_classify_leaves_an_unrecognised_title_unclassified() -> None:
    """A title no rule matches is `None` — an honest gap, never a catch-all bucket (§18)."""
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Zookeeper and Falconer")

    classification = await service.classify(USER, OPPORTUNITY, now=T0)

    assert classification.role_family is None
    assert classification.provenance is RoleFamilyProvenance.DETERMINISTIC_TITLE


async def test_classify_preserves_the_first_created_at_on_a_re_run() -> None:
    """The row records when the role was first classified, not when the backfill last ran."""
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Data Scientist")

    first = await service.classify(USER, OPPORTUNITY, now=T0)
    second = await service.classify(USER, OPPORTUNITY, now=T1)

    assert first.id == second.id
    assert second.created_at == T0
    assert second.updated_at == T1


async def test_classify_never_overwrites_a_manual_classification() -> None:
    """A human's correction outranks the rule and the next backfill must not undo it (§18)."""
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Senior Software Engineer")
    await service.set_manual(USER, OPPORTUNITY,
                            role_family=RoleFamily.PRODUCT_MANAGEMENT, now=T0)

    reclassified = await service.classify(USER, OPPORTUNITY, now=T1)

    assert reclassified.role_family is RoleFamily.PRODUCT_MANAGEMENT
    assert reclassified.provenance is RoleFamilyProvenance.MANUAL
    assert reclassified.updated_at == T0      # untouched by the backfill


async def test_classifying_a_missing_opportunity_is_not_found() -> None:
    service, _, _ = build_service()

    with pytest.raises(CareerError) as refusal:
        await service.classify(USER, OPPORTUNITY, now=T0)

    assert refusal.value.code is CareerErrorCode.OPPORTUNITY_NOT_FOUND


# --- the manual correction --------------------------------------------------------------


async def test_set_manual_records_the_named_family_with_manual_provenance() -> None:
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Data Scientist")

    classification = await service.set_manual(
        USER, OPPORTUNITY, role_family=RoleFamily.DATA_AND_ANALYTICS, now=T0)

    assert classification.role_family is RoleFamily.DATA_AND_ANALYTICS
    assert classification.provenance is RoleFamilyProvenance.MANUAL
    assert classification.is_manual


async def test_set_manual_preserves_a_prior_deterministic_created_at() -> None:
    """Correcting a machine guess keeps the row's birthday; only `updated_at` moves."""
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Senior Software Engineer")
    deterministic = await service.classify(USER, OPPORTUNITY, now=T0)

    corrected = await service.set_manual(
        USER, OPPORTUNITY, role_family=RoleFamily.DESIGN, now=T1)

    assert corrected.id == deterministic.id
    assert corrected.created_at == T0
    assert corrected.updated_at == T1


async def test_setting_a_manual_family_on_a_missing_opportunity_is_not_found() -> None:
    service, _, _ = build_service()

    with pytest.raises(CareerError) as refusal:
        await service.set_manual(USER, OPPORTUNITY,
                                role_family=RoleFamily.SALES, now=T0)

    assert refusal.value.code is CareerErrorCode.OPPORTUNITY_NOT_FOUND


# --- ownership and listing --------------------------------------------------------------


async def test_classification_is_owner_scoped() -> None:
    """One account's classification of a shared posting is invisible to another (§18-20)."""
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Data Scientist")
    await service.set_manual(USER, OPPORTUNITY,
                            role_family=RoleFamily.DATA_AND_ANALYTICS, now=T0)

    assert await service.classification(USER, OPPORTUNITY) is not None
    assert await service.classification(OTHER_USER, OPPORTUNITY) is None


async def test_two_accounts_classify_the_same_posting_independently() -> None:
    """Each candidate files the shared posting under their own family, on their own row."""
    service, roles, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Senior Software Engineer")

    mine = await service.set_manual(USER, OPPORTUNITY,
                                   role_family=RoleFamily.SOFTWARE_ENGINEERING, now=T0)
    theirs = await service.set_manual(OTHER_USER, OPPORTUNITY,
                                     role_family=RoleFamily.PRODUCT_MANAGEMENT, now=T0)

    assert mine.id != theirs.id
    assert len(roles.classifications) == 2


async def test_list_for_user_returns_only_this_accounts_classifications() -> None:
    service, _, opportunities = build_service()
    await _seed_opportunity(opportunities, title="Senior Software Engineer")
    await _seed_opportunity(opportunities, opportunity_id=OTHER_OPPORTUNITY,
                            title="Data Scientist", external_id="posting-2",
                            fingerprint="fingerprint-2")
    await service.classify(USER, OPPORTUNITY, now=T0)
    await service.classify(USER, OTHER_OPPORTUNITY, now=T1)
    await service.classify(OTHER_USER, OPPORTUNITY, now=T2)

    mine = await service.list_for_user(USER)

    assert len(mine) == 2
    assert {c.user_id for c in mine} == {USER}
    # Most recently updated first — the second classification leads.
    assert mine[0].opportunity_id == OTHER_OPPORTUNITY
