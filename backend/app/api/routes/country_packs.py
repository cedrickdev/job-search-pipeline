"""Read-only catalogue of the Country Packs active in this deployment."""
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import CurrentSession, country_packs
from backend.app.api.schemas import CountryPackListResponse, CountryPackResponse
from country_packs.registry import CountryPackRegistry

router = APIRouter(prefix="/country-packs", tags=["v2-country-packs"])


@router.get("", response_model=CountryPackListResponse)
async def list_country_packs(
    current: CurrentSession,
    registry: Annotated[CountryPackRegistry, Depends(country_packs)],
) -> CountryPackListResponse:
    """Public pack metadata for authenticated UI clients; never source credentials."""
    return CountryPackListResponse(
        packs=tuple(CountryPackResponse.of(pack) for pack in registry.packs))
