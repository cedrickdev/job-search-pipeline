"""Discovery orchestrator: sweep all sources, filter, insert, report health.

Per the spec, a failing source must be VISIBLE: each source gets an entry in
the health dict ({ok, found, new, errors}) and errors never abort the run.
"""
import argparse
import json
from datetime import datetime, timezone

import yaml

from pipeline import paths
from pipeline.db import connect, init_db
from pipeline.filters import load_searches, matches_scope
from pipeline.http_fetch import FetchError
from pipeline.jobs import insert_job
from pipeline.sources import (ashby, greenhouse, lever, linkedin, wtj,
                              indeed_ch, jobup, jooble, migros, coop, jobscout24,
                              manpower)
from pipeline.statuses import create_application

ATS_SOURCES = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
}
QUERY_SOURCES = {
    "wtj": wtj.search_jobs,
    "linkedin": linkedin.search_jobs,
    # "indeed" (fr.indeed.com, France-only) is deliberately excluded here:
    # Cédrick's search is Switzerland-only and indeed.py has no location
    # targeting for CH, so it returns France-wide noise regardless of the
    # `locations` passed. indeed_ch.py (ch-fr.indeed.com) replaces it.
    "indeed_ch": indeed_ch.search_jobs,
    "jobup": jobup.search_jobs,
    "jooble": jooble.search_jobs,
    "migros": migros.search_jobs,
    "coop": coop.search_jobs,
    "jobscout24": jobscout24.search_jobs,
    "manpower": manpower.search_jobs,
}
DESCRIPTION_FETCHERS = {
    "wtj": wtj.fetch_description,
    "linkedin": linkedin.fetch_description,
}
DESCRIPTION_FETCH_CAP = 25

DEFAULT_LOOKBACK = 3
MAX_LOOKBACK = 7


def load_companies() -> dict:
    with open(paths.COMPANIES_PATH) as f:
        return yaml.safe_load(f) or {}


def lookback_days(conn, default: int = DEFAULT_LOOKBACK,
                  cap: int = MAX_LOOKBACK) -> int:
    """Days to look back: covers the gap since the last finished discovery run
    (spec §6 missed-run handling), capped to avoid re-sweeping the world."""
    row = conn.execute(
        "SELECT started_at FROM runs WHERE kind = 'discovery'"
        " AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return default
    last = datetime.fromisoformat(row["started_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    gap = (datetime.now(timezone.utc) - last).days + 1
    return min(max(gap, default), cap)


def run_discovery(conn, searches: dict, companies: dict, lookback: int) -> dict:
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    health: dict[str, dict] = {}
    new_jobs = 0

    for ats, fetcher in ATS_SOURCES.items():
        report = health.setdefault(
            ats, {"ok": True, "found": 0, "new": 0, "errors": []})
        for entry in companies.get(ats) or []:
            try:
                found = fetcher(entry["token"], entry["company"])
            except FetchError as exc:
                report["ok"] = False
                report["errors"].append(f"{entry['token']}: {exc}")
                continue
            report["found"] += len(found)
            report["new"] += _ingest(conn, found, searches)
        new_jobs += report["new"]

    for name, searcher in QUERY_SOURCES.items():
        report = health.setdefault(
            name, {"ok": True, "found": 0, "new": 0, "errors": []})
        for query in searches.get("queries", []):
            for location in searches.get("locations", []):
                try:
                    found = searcher(query, location, lookback_days=lookback)
                except FetchError as exc:
                    report["ok"] = False
                    report["errors"].append(f"{query} @ {location}: {exc}")
                    continue
                report["found"] += len(found)
                report["new"] += _ingest(conn, found, searches)
        new_jobs += report["new"]

    _backfill_descriptions(conn)

    summary = {"lookback_days": lookback, "new_jobs": new_jobs, "health": health}
    record_run(conn, "discovery", started_at, json.dumps(summary))
    return summary


def _ingest(conn, found: list[dict], searches: dict) -> int:
    new = 0
    for job in found:
        if not matches_scope(job, searches):
            continue
        job_id, created = insert_job(conn, job)
        if created:
            create_application(conn, job_id, source="discovery")
            new += 1
    return new


def _backfill_descriptions(conn) -> None:
    """Fetch missing descriptions for sources whose search results are thin.
    Capped per run; failures skip silently (the job stays, scored from title)."""
    rows = conn.execute(
        "SELECT id, source, url FROM jobs"
        " WHERE description IS NULL AND url IS NOT NULL"
        " ORDER BY id DESC LIMIT ?", (DESCRIPTION_FETCH_CAP,)).fetchall()
    for row in rows:
        fetcher = DESCRIPTION_FETCHERS.get(row["source"])
        if fetcher is None:
            continue
        try:
            description = fetcher(row["url"])
        except FetchError:
            continue
        if description:
            conn.execute("UPDATE jobs SET description = ? WHERE id = ?",
                         (description, row["id"]))
            conn.commit()


def record_run(conn, kind: str, started_at: str, summary: str) -> None:
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at, summary)"
        " VALUES (?, ?, ?, ?)", (kind, started_at, finished_at, summary))
    conn.commit()


def probe(companies: dict) -> None:
    """Print OK/FAIL per configured ATS board. For seed-list maintenance."""
    from pipeline.http_fetch import fetch_json
    apis = {"greenhouse": greenhouse.API, "lever": lever.API, "ashby": ashby.API}
    for ats, entries in companies.items():
        for entry in entries or []:
            url = apis[ats].format(token=entry["token"])
            try:
                fetch_json(url, params={"mode": "json"} if ats == "lever" else None)
                print(f"OK   {ats:11} {entry['token']}")
            except FetchError as exc:
                print(f"FAIL {ats:11} {entry['token']}: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep job sources into the DB.")
    parser.add_argument("--probe", action="store_true",
                        help="check configured ATS boards and exit")
    parser.add_argument("--lookback-days", type=int, default=None)
    args = parser.parse_args()

    if args.probe:
        probe(load_companies())
        raise SystemExit(0)

    connection = connect(paths.DB_PATH)
    init_db(connection)
    lookback = args.lookback_days or lookback_days(connection)
    result = run_discovery(connection, load_searches(), load_companies(), lookback)
    print(json.dumps(result, indent=2))
