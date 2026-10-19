"""The authenticated, read-only Country Pack catalogue exposed to the frontend."""

import pytest

from tests.v2_api import api_harness


@pytest.mark.asyncio
async def test_country_pack_catalogue_requires_a_session(tmp_path):
    async with api_harness(tmp_path) as api:
        response = await api.read("/country-packs")

    assert response.status_code == 401
    assert response.json()["error"] == "not_authenticated"


@pytest.mark.asyncio
async def test_country_pack_catalogue_exposes_only_the_active_swiss_pack(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read("/country-packs")

    assert response.status_code == 200
    assert response.json() == {
        "packs": [{
            "country": "CH",
            "display_name": "Switzerland",
            "default_locale": "fr-CH",
            "locales": ["fr-CH", "de-CH", "it-CH", "en-CH"],
            "currency": "CHF",
            "languages": ["fr", "de", "it", "en"],
            "timezone": "Europe/Zurich",
            "map_viewport": {
                "center": {"latitude": 46.8182, "longitude": 8.2275},
                "zoom": 6.2,
                "bounds": {
                    "north": 47.8085,
                    "south": 45.8179,
                    "east": 10.4921,
                    "west": 5.9559,
                },
            },
            "opportunity_types": [
                "FULL_TIME", "PART_TIME", "STUDENT_JOB", "INTERNSHIP",
                "APPRENTICESHIP", "WORK_STUDY", "GRADUATE", "TEMPORARY",
                "FREELANCE",
            ],
            "enabled_source_count": 11,
        }],
    }
    assert "api_key" not in response.text
    assert "config_env_vars" not in response.text
    assert "permit" not in response.text.lower()
