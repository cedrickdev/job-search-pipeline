"""Employers an operator already configured in V1, read as companies (§8).

`config/companies.yaml` is the file V1's three ATS adapters sweep. Every line in it
is an assertion that a named employer publishes on a named platform under a named
organization — which is exactly a company seed, and §8 asks that it be reused rather
than copied into a parallel Python list. So this provider imports V1's own loader,
`discovery.adapters.v1_sources.load_v1_companies`, and reads the same file at the
same path.

What that buys, concretely: an operator adds `- token: acme, company: Acme SA` under
`greenhouse:`, and Acme appears in company search **with zero active opportunities**
— acceptance criterion §28 — because this provider never looks at the postings table.
The board is not fetched. Nothing is fetched.

Confidence is `LIKELY` throughout, and `detect_from_configuration` explains why: the
loader skips malformed lines and validates nothing about the ones it keeps, so a
token in that file is a claim. `CONFIRMED` is reserved for a URL out of the
platform's own address space.
"""
from collections.abc import Mapping, Sequence

from backend.app.companies.ats import (
    SOURCE_KEY_PLATFORMS,
    detect_from_configuration,
    platform_for_source_key,
)
from backend.app.companies.contracts import (
    CompanyDiscoveryCapability,
    CompanyDiscoveryRequest,
    CompanyProviderMetadata,
    CompanyProviderType,
    CompanySeed,
    DiscoveredCareerSite,
    DiscoveredCompany,
)
from backend.app.companies.providers.base import (
    Clock,
    LocalCompanyProvider,
    SeedBatch,
    utc_now,
)
from backend.app.discovery.adapters.v1_sources import CompanyBoard, load_v1_companies
from backend.app.domain.company import (
    CompanySeedKind,
    DetectionStatus,
    normalize_company_name,
)

PROVIDER_KEY = "configured_ats"

# A board loader, so a test can hand over two employers without writing a YAML file
# and production can hand over V1's file. Deliberately the same shape
# `build_v1_sources` accepts, so `bootstrap` can pass one value to both.
BoardMap = Mapping[str, Sequence[CompanyBoard]]


def metadata() -> CompanyProviderMetadata:
    """What this provider claims. Country-agnostic, and priority 10.

    First of the three: a configured employer is the strongest seed Phase 6 has —
    somebody typed it on purpose, and it carries an ATS organization — so the
    companies it creates are the ones later providers resolve *against* rather than
    duplicate. Ordering is not correctness (resolution is what prevents duplicates)
    but it makes a pass's provenance read the way an operator expects.

    No `COUNTRY_FILTER`: `config/companies.yaml` says nothing about where an employer
    is, and inventing a country for a line in it would be a fabricated fact. The
    orchestrator turns that missing capability into a `COUNTRY_FILTER_NOT_SUPPORTED`
    warning whenever a request names a country, which is the honest report.
    """
    return CompanyProviderMetadata(
        provider_key=PROVIDER_KEY,
        display_name="Configured ATS boards",
        provider_type=CompanyProviderType.CONFIGURATION,
        capabilities=frozenset({
            CompanyDiscoveryCapability.ATS_DETECTION,
            CompanyDiscoveryCapability.CAREER_SITE_DISCOVERY,
            CompanyDiscoveryCapability.HEALTHCHECK,
        }),
        priority=10,
        notes="Reads config/companies.yaml, the file V1's greenhouse, lever and "
              "ashby adapters already sweep. Makes no request.")


class ConfiguredAtsCompanyProvider(LocalCompanyProvider):
    """Every employer named in `config/companies.yaml`, as a company seed."""

    def __init__(self, *, boards: BoardMap | None = None,
                 clock: Clock = utc_now) -> None:
        """`boards=None` means V1's file, read on every pass.

        Read per pass rather than cached at construction, because an operator edits
        that file and expects the next run to see it — the same reading V1 has, since
        `load_v1_companies` is called inside its adapters' work too. The file is
        small and local; a cache here would only add a restart requirement.
        """
        super().__init__(metadata=metadata(), clock=clock)
        self._boards = boards

    def _load(self) -> BoardMap:
        return load_v1_companies() if self._boards is None else self._boards

    def _seeds(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        """One company per configured board, in a deterministic order.

        Order is platform key then file order, both stable, because §23 asks that two
        passes over an unchanged file produce the same result — including which
        entries a `limit` cuts.

        A key that is not one of the three known platforms is skipped and reported,
        not ignored: `workday: [...]` in that file means an operator expects a board
        to be swept that nothing sweeps, and silence would leave them waiting.
        """
        boards = self._load()
        companies: list[DiscoveredCompany] = []
        skipped: list[str] = []
        observed_at = self._clock()

        for source_key in sorted(boards):
            platform = platform_for_source_key(source_key)
            if platform is None:
                skipped.append(
                    f"config/companies.yaml lists {source_key!r}, which is not one of "
                    f"the supported platforms ({', '.join(sorted(SOURCE_KEY_PLATFORMS))}"
                    "); its entries were not read")
                continue
            for board in boards[source_key]:
                if not normalize_company_name(board.company):
                    skipped.append(
                        f"a {source_key} entry with token {board.token!r} has a name "
                        "that normalizes to nothing, so it could never be compared "
                        "against a stored company")
                    continue
                detection = detect_from_configuration(
                    platform, board.token, detected_by=PROVIDER_KEY,
                    observed_at=observed_at)
                companies.append(DiscoveredCompany(
                    seed=CompanySeed(
                        kind=CompanySeedKind.ATS_ORGANIZATION,
                        name=board.company,
                        # The platform and token, not the name: the same employer
                        # renamed in that file must stay the same discovery record
                        # (§23), and the pair is what V1 actually fetches with.
                        external_id=f"{platform.value}:{board.token}",
                        careers_url=detection.board_url,
                        ats_platform=platform,
                        ats_organization_id=board.token,
                        source_url=detection.board_url,
                        raw={"source_key": source_key, "token": board.token}),
                    ats_platform=platform,
                    ats_organization_id=board.token,
                    ats_status=detection.status,
                    ats_evidence=detection.detected.evidence,
                    career_sites=(DiscoveredCareerSite(
                        url=detection.board_url,
                        kind=detection.site_kind,
                        platform=platform,
                        verification_status=detection.status),),
                    confidence=DetectionStatus.LIKELY))

        return SeedBatch(
            companies, skipped=skipped,
            empty_detail="config/companies.yaml configures no ATS board; it ships "
                         "empty, so this is the expected state until an operator adds "
                         "an employer")
