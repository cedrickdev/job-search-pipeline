# tests/test_v2_api_roles.py
"""`/api/v2` — classer une opportunité par famille de rôle, vu par un navigateur.

Les tests de service (`test_v2_career_roles.py`) tranchent les décisions ; celui-ci tient la
couche route et son câblage. La règle de la phase se lit au fil : la famille est proposée par une
règle *déterministe* sur le titre — jamais un provider — et un humain peut la corriger, la
correction l'emportant sur tout rétro-remplissage ultérieur (§18, §47).

La classification est propre au compte, même si le posting est un fait partagé : deux candidats
peuvent classer le même posting différemment, et chacun ne voit que le sien. L'opportunité, elle,
est nommée dans le chemin ; une manquante est un 404 `opportunity_not_found`, et une lecture sans
classement pour ce compte un 404 `role_classification_not_found` — jamais un 500 à rendre du vide.
"""
import pytest

from tests.v2_api import api_harness
from tests.v2_builders import OPPORTUNITY, an_opportunity

pytestmark = pytest.mark.asyncio

# A title the deterministic keyword rule places squarely, and its resulting family — so a test
# asserts the rule ran without re-encoding the whole table.
ENGINEER_TITLE = "Senior Software Engineer"
ENGINEER_FAMILY = "SOFTWARE_ENGINEERING"
# A title no keyword rule matches, so the honest verdict is the unclassified `None` cell (§18).
UNPLACEABLE_TITLE = "Chief Vibes Officer"


async def _seed_opportunity(api, *, title=ENGINEER_TITLE, opportunity_id=OPPORTUNITY):
    """Seed the shared posting the classification reads its title from."""
    await api.postings.upsert(an_opportunity(id=opportunity_id, title=title))


async def _classify(api, opportunity_id=OPPORTUNITY):
    """POST the deterministic classify; the response is returned for a status check."""
    return await api.write(
        "POST", f"/opportunities/{opportunity_id}/role-classification")


# --- the deterministic classify --------------------------------------------

async def test_classifying_a_known_title_returns_its_family_owner_free(tmp_path):
    """A recognised title is placed by the rule and stamped deterministic — no `user_id`."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        response = await _classify(api)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role_family"] == ENGINEER_FAMILY
        assert body["provenance"] == "DETERMINISTIC_TITLE"
        assert body["is_manual"] is False
        assert body["opportunity_id"] == str(OPPORTUNITY)
        assert "user_id" not in body

async def test_classifying_an_unplaceable_title_is_an_honest_null_not_a_catch_all(tmp_path):
    """A title no rule matches is `role_family: null` — an honest gap, never a fallback family."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api, title=UNPLACEABLE_TITLE)
        response = await _classify(api)
        assert response.status_code == 200, response.text
        assert response.json()["role_family"] is None

async def test_classifying_a_missing_opportunity_is_404(tmp_path):
    """The posting is a shared fact named in the path, so a missing one is a 404."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await _classify(api)
        assert response.status_code == 404
        assert response.json()["error"] == "opportunity_not_found"

async def test_classify_is_idempotent_and_keeps_the_first_created_at(tmp_path):
    """Re-classifying the same posting is safe: the same row, its first `created_at` preserved."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        first = await _classify(api)
        again = await _classify(api)
        assert first.status_code == again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert again.json()["created_at"] == first.json()["created_at"]


# --- the human correction, and the read -------------------------------------

async def test_a_manual_classification_outranks_the_rule_and_is_marked_manual(tmp_path):
    """A PUT records a human's family, stamped `MANUAL`, and a later classify leaves it alone."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)  # the rule would say SOFTWARE_ENGINEERING
        put = await api.write(
            "PUT", f"/opportunities/{OPPORTUNITY}/role-classification",
            json={"role_family": "PRODUCT_MANAGEMENT"})
        assert put.status_code == 200, put.text
        assert put.json()["role_family"] == "PRODUCT_MANAGEMENT"
        assert put.json()["provenance"] == "MANUAL"
        assert put.json()["is_manual"] is True
        # The deterministic backfill must not overwrite the human's choice.
        after = await _classify(api)
        assert after.json()["role_family"] == "PRODUCT_MANAGEMENT"
        assert after.json()["is_manual"] is True

async def test_setting_a_manual_family_for_a_missing_opportunity_is_404(tmp_path):
    """A PUT against a posting that does not exist is a 404, not a stored orphan."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.write(
            "PUT", f"/opportunities/{OPPORTUNITY}/role-classification",
            json={"role_family": "DESIGN"})
        assert response.status_code == 404
        assert response.json()["error"] == "opportunity_not_found"

async def test_setting_an_unknown_family_is_422(tmp_path):
    """A family outside the closed vocabulary is refused at the boundary — a 422."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        response = await api.write(
            "PUT", f"/opportunities/{OPPORTUNITY}/role-classification",
            json={"role_family": "WIZARDRY"})
        assert response.status_code == 422

async def test_reading_an_unclassified_opportunity_is_404(tmp_path):
    """A read before any classification is a 404 `role_classification_not_found` — never .of(None)."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        response = await api.read(f"/opportunities/{OPPORTUNITY}/role-classification")
        assert response.status_code == 404
        assert response.json()["error"] == "role_classification_not_found"

async def test_reading_after_classifying_returns_the_verdict(tmp_path):
    """Once classified, the GET returns this account's verdict for the posting."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        await _classify(api)
        response = await api.read(f"/opportunities/{OPPORTUNITY}/role-classification")
        assert response.status_code == 200, response.text
        assert response.json()["role_family"] == ENGINEER_FAMILY


# --- the list, and its ownership scope --------------------------------------

async def test_the_list_resolves_before_the_opportunity_route_and_scopes_to_the_owner(tmp_path):
    """`/role-classifications` is a literal, and it holds only this account's verdicts.

    A 200 carrying a `classifications` list proves the plural literal is not parsed as an
    opportunity id (which would be a 422 UUID failure), and a second account sees an empty list —
    one account's classification of a shared posting never leaks into another's.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _seed_opportunity(api)
        await _classify(api)
        mine = await api.read("/role-classifications")
        assert mine.status_code == 200, mine.text
        assert len(mine.json()["classifications"]) == 1
        assert mine.json()["classifications"][0]["role_family"] == ENGINEER_FAMILY
        await api.register(email="someone.else@example.com")  # switches accounts
        theirs = await api.read("/role-classifications")
        assert theirs.status_code == 200
        assert theirs.json()["classifications"] == []
