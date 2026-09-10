"""Recognising which applicant tracking system an employer publishes through.

Three platforms, exactly the three V1 has adapters for (§9). Detection works on
**URL structure and configuration only** — no page fetch, no HTML scan, no
Playwright. That is a constraint from the phase order and also the honest limit of
what this phase can claim: a string found in a page body is weak evidence, and
§10 asks the difference between strong and weak to be typed rather than assumed.

The evidence hierarchy, as implemented:

- A URL on the platform's own host with the organization identifier in the path is
  `CONFIRMED`. `boards.greenhouse.io/acme` is not a hint; it is Greenhouse's own
  address for Acme's board, and the token needed to fetch it is right there.
- A configured organization identifier — an operator's line in
  `config/companies.yaml` — is `LIKELY`. Somebody asserted it, nobody verified it,
  and V1's own loader accepts whatever is typed.
- Everything else is `None`: no detection, rather than a `DetectedATS` whose status
  is `UNKNOWN`. The domain model refuses that construction on purpose.

What deliberately produces nothing: a mention of the platform's name in text. It
is the case §10 names as insufficient, and rather than represent it as `LIKELY`
this module declines to look at page bodies at all — there is no code path here
that could be pointed at one.
"""
import re
from collections.abc import Iterable, Mapping, Sequence

from backend.app.domain.base import UtcDatetime
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    DetectedATS,
    DetectionStatus,
    Evidence,
    normalize_domain,
)

# The organization identifier as each platform writes it in a URL: the "token" V1
# calls it. Bounded and conservative — a path segment that is not this shape is not
# a board token, and reading one out of an arbitrary URL is how a detector starts
# reporting `/careers/engineering` as an organization.
_ORGANIZATION = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"

# Host → (platform, path pattern). One entry per host the platform actually serves
# boards on, because a company's URL is whichever one its ATS handed it: Greenhouse
# has moved from `boards` to `job-boards` and both are live, and the API hosts turn
# up in configuration exported from a previous integration.
#
# Ordered longest-host-first at use, so `job-boards.greenhouse.io` is tested before
# any future bare `greenhouse.io` entry could shadow it.
_BOARD_HOSTS: tuple[tuple[str, AtsPlatform, re.Pattern[str]], ...] = (
    ("boards.greenhouse.io", AtsPlatform.GREENHOUSE,
     re.compile(rf"^/(?:embed/job_board/?\?for=)?(?P<token>{_ORGANIZATION})")),
    ("job-boards.greenhouse.io", AtsPlatform.GREENHOUSE,
     re.compile(rf"^/(?P<token>{_ORGANIZATION})")),
    ("boards-api.greenhouse.io", AtsPlatform.GREENHOUSE,
     re.compile(rf"^/v1/boards/(?P<token>{_ORGANIZATION})")),
    ("api.greenhouse.io", AtsPlatform.GREENHOUSE,
     re.compile(rf"^/v1/boards/(?P<token>{_ORGANIZATION})")),
    ("jobs.lever.co", AtsPlatform.LEVER,
     re.compile(rf"^/(?P<token>{_ORGANIZATION})")),
    ("api.lever.co", AtsPlatform.LEVER,
     re.compile(rf"^/v0/postings/(?P<token>{_ORGANIZATION})")),
    ("jobs.ashbyhq.com", AtsPlatform.ASHBY,
     re.compile(rf"^/(?P<token>{_ORGANIZATION})")),
    ("api.ashbyhq.com", AtsPlatform.ASHBY,
     re.compile(rf"^/posting-api/job-board/(?P<token>{_ORGANIZATION})")),
)

# The board URL to publish for an organization, per platform. The canonical public
# one, not the API endpoint: a `CareerSite` row is something a person may click, and
# `boards-api.greenhouse.io/v1/boards/acme/jobs` returns JSON to a browser.
_BOARD_URL_TEMPLATES: Mapping[AtsPlatform, str] = {
    AtsPlatform.GREENHOUSE: "https://boards.greenhouse.io/{token}",
    AtsPlatform.LEVER: "https://jobs.lever.co/{token}",
    AtsPlatform.ASHBY: "https://jobs.ashbyhq.com/{token}",
}

# V1's `config/companies.yaml` keys, which are also the registered `source_key` of
# the three ATS adapters. One mapping rather than two constants, so the day a fourth
# board is wrapped the correspondence is stated in one place (§8: reuse the existing
# configuration, do not copy it into a parallel Python list).
SOURCE_KEY_PLATFORMS: Mapping[str, AtsPlatform] = {
    "greenhouse": AtsPlatform.GREENHOUSE,
    "lever": AtsPlatform.LEVER,
    "ashby": AtsPlatform.ASHBY,
}

_EVIDENCE_FROM_URL = "ATS_BOARD_URL"
_EVIDENCE_FROM_CONFIG = "ATS_CONFIGURED_ORGANIZATION"


def board_url(platform: AtsPlatform, organization_id: str) -> str:
    """The public board URL for an organization on a platform."""
    return _BOARD_URL_TEMPLATES[platform].format(token=organization_id)


def platform_for_source_key(source_key: str) -> AtsPlatform | None:
    """The platform an ATS source plugin reads, or `None` for any other source."""
    return SOURCE_KEY_PLATFORMS.get(source_key)


class AtsDetection:
    """A detected platform plus the careers endpoint it implies.

    A tiny class rather than a `DomainModel` because it is a return value passed
    straight into a service and never persisted or compared: `DetectedATS` is the
    part that gets stored, and `board_url`/`site_kind` are what a `CareerSite` row
    is built from. Making it a frozen model would add validation of fields nobody
    validates and a second name for the same platform value.
    """

    __slots__ = ("board_url", "detected", "site_kind")

    def __init__(self, detected: DetectedATS, board_url: str,
                 site_kind: CareerSiteKind = CareerSiteKind.ATS_BOARD) -> None:
        self.detected = detected
        self.board_url = board_url
        self.site_kind = site_kind

    @property
    def platform(self) -> AtsPlatform:
        return self.detected.platform

    @property
    def organization_id(self) -> str | None:
        return self.detected.organization_id

    @property
    def status(self) -> DetectionStatus:
        return self.detected.status


def detect_from_url(url: str, *, detected_by: str,
                    observed_at: UtcDatetime | None = None) -> AtsDetection | None:
    """The platform and organization a URL names, or `None` if it names neither.

    `CONFIRMED`, because the identifier came out of the platform's own address
    space: whoever wrote this URL — a company on its careers page, a redirect a
    server sent — was addressing a specific board, and the token is fetchable.

    A platform host with no usable token in the path returns `None` rather than a
    platform with `organization_id=None`: `boards.greenhouse.io/` alone identifies
    nobody, and a detection nothing can act on is worse than no detection because it
    looks like progress.
    """
    host = normalize_domain(url)
    if not host:
        return None
    path = _path_of(url)
    for board_host, platform, pattern in sorted(_BOARD_HOSTS,
                                                key=lambda entry: -len(entry[0])):
        if host != board_host and not host.endswith(f".{board_host}"):
            continue
        match = pattern.match(path)
        if match is None:
            return None
        token = match.group("token")
        # `www` and `embed` are path furniture on Greenhouse's older board host, not
        # organizations. Rejecting them explicitly beats widening `_ORGANIZATION`, which
        # would also stop matching real tokens.
        if token in {"embed", "www"}:
            return None
        detected = DetectedATS(
            platform=platform,
            organization_id=token,
            status=DetectionStatus.CONFIRMED,
            detected_by=detected_by,
            evidence=(Evidence(
                code=_EVIDENCE_FROM_URL,
                detail=f"{board_host} addresses this board directly, so "
                       f"{platform.value} organization {token!r} is the employer's "
                       "own identifier on that platform",
                source_url=_https(url),
                observed_at=observed_at),),
        )
        return AtsDetection(detected=detected,
                            board_url=board_url(platform, token))
    return None


def detect_from_configuration(platform: AtsPlatform, organization_id: str, *,
                              detected_by: str,
                              observed_at: UtcDatetime | None = None,
                              ) -> AtsDetection:
    """The platform an operator configured, at `LIKELY` (§10).

    Not `CONFIRMED`: `config/companies.yaml` is a hand-maintained file whose loader
    skips malformed lines and validates nothing about the ones it keeps, so a token
    in it is a claim and not an observation. The board URL is still constructed —
    it is exactly what V1 fetches — and the phase that fetches it can promote the
    status.
    """
    url = board_url(platform, organization_id)
    detected = DetectedATS(
        platform=platform,
        organization_id=organization_id,
        status=DetectionStatus.LIKELY,
        detected_by=detected_by,
        evidence=(Evidence(
            code=_EVIDENCE_FROM_CONFIG,
            detail=f"an operator configured {platform.value} organization "
                   f"{organization_id!r} for this employer; nothing has fetched the "
                   "board yet, so the claim is unverified",
            source_url=url,
            observed_at=observed_at),),
    )
    return AtsDetection(detected=detected, board_url=url)


def detect_from_urls(urls: Iterable[str], *, detected_by: str,
                     observed_at: UtcDatetime | None = None,
                     ) -> AtsDetection | None:
    """The first ATS any of `urls` names, preferring the firmest detection.

    "First" is by confidence and then by input order, so a company whose careers
    page is listed before its board still ends up with the board's detection. Input
    order breaks the remaining ties, which keeps the result deterministic for a
    given seed — a requirement of §23, since an unstable choice here would rewrite
    the ATS columns on alternate sweeps.
    """
    found = [detection for url in urls
             if (detection := detect_from_url(url, detected_by=detected_by,
                                              observed_at=observed_at)) is not None]
    if not found:
        return None
    ranking = {DetectionStatus.CONFIRMED: 0, DetectionStatus.LIKELY: 1,
               DetectionStatus.UNKNOWN: 2}
    return min(enumerate(found),
               key=lambda pair: (ranking[pair[1].status], pair[0]))[1]


def _path_of(url: str) -> str:
    """The path and query of a URL, in the form the board patterns expect.

    Written by hand rather than with `urlsplit` because Greenhouse's embed form
    puts the token in the query string (`?for=acme`), so the pattern has to see
    both parts as one string.
    """
    without_scheme = url.split("://", 1)[-1]
    slash = without_scheme.find("/")
    if slash == -1:
        return ""
    return without_scheme[slash:]


def _https(url: str) -> str:
    """`url` with a scheme, since `Evidence.source_url` requires a fetchable one.

    A configured value is routinely written `boards.greenhouse.io/acme`, and the
    domain refuses that as a URL — correctly, since nothing can fetch it. Adding
    `https://` asserts nothing that was not already true of every host here: all
    three platforms are HTTPS-only.
    """
    return url if "://" in url else f"https://{url}"


def platforms_from_source_keys(source_keys: Sequence[str]) -> tuple[AtsPlatform, ...]:
    """The platforms among a list of source keys, deduplicated, in a stable order.

    Used by the configured-ATS provider to turn `config/companies.yaml`'s top-level
    keys into platforms without hard-coding the correspondence twice.
    """
    seen: dict[AtsPlatform, None] = {}
    for key in source_keys:
        platform = SOURCE_KEY_PLATFORMS.get(key)
        if platform is not None:
            seen.setdefault(platform, None)
    return tuple(seen)
