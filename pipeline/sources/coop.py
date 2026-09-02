"""Coop (via the "Prospective" recruiting platform, ohws.prospective.ch).
Public JSON API, no auth needed. The API's own text relevance is loose for
multi-word queries (it can return most of the database), so results are
always filtered client-side on the canton attribute (id "30") rather than
trusted from the query match alone."""
from pipeline.http_fetch import fetch
from pipeline.sources._common import normalize

API = "https://ohws.prospective.ch/public/v1/medium/1000103/jobs"
CANTON_ATTR = "30"


def search_jobs(query: str, location: str, lookback_days: int = 3) -> list[dict]:
    resp = fetch(API, params={"lang": "fr", "q": query, "offset": 0, "limit": 50})
    canton = _canton_from_location(location)
    return _parse_jobs(resp.json(), canton)


def _parse_jobs(data: dict, canton: str | None) -> list[dict]:
    jobs = []
    for r in data.get("jobs", []):
        cantons = r.get("attributes", {}).get(CANTON_ATTR) or []
        if canton and canton not in cantons:
            continue
        szas = r.get("szas", {})
        pct = szas.get("sza_pensum.max")
        jobs.append(normalize(
            source="coop",
            company="Coop",
            title=r.get("title") or "",
            url=szas.get("sza_apply_link"),
            location=", ".join(cantons) or None,
            contract_type=r.get("attributes", {}).get("40", [None])[0],
            salary=f"{pct}%" if pct else None,
            description=_strip(szas.get("sza_tasks")),
        ))
    return jobs


_VAUD_HINTS = ("vaud", "yverdon", "lausanne", "nyon", "morges", "montreux",
              "vevey", "renens", "mauborget")


def _canton_from_location(location: str) -> str | None:
    """This pipeline only ever searches Vaud-area locations (see
    config/searches.yaml); any of them should resolve to canton Vaud."""
    if not location:
        return None
    loc = location.lower()
    if any(hint in loc for hint in _VAUD_HINTS):
        return "Vaud"
    for canton in ("Genève", "Fribourg", "Neuchâtel", "Valais"):
        if canton.lower() in loc:
            return canton
    return None


def _strip(html: str | None) -> str | None:
    if not html:
        return None
    from pipeline.sources._common import strip_html
    return strip_html(html)
