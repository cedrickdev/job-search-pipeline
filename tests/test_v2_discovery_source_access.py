"""Live Swiss discovery must stay closed until an operator reviews sources."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from backend.app.discovery.ats_board_catalog import load_reviewed_boards
from backend.app.services.discovery_source_access import DiscoverySourceAccessDenied, SourceAccessPolicy
from backend.app.services.discovery_catalog import DiscoveryEnqueueService
from tests.v2_builders import SEARCH_PROFILE, USER, a_search_profile


class Searches:
    def __init__(self, profile):
        self.profile = profile

    async def get(self, user_id, profile_id):
        return self.profile if (user_id, profile_id) == (USER, SEARCH_PROFILE) else None


class Dispatcher:
    def __init__(self):
        self.tasks = []

    async def enqueue(self, task):
        self.tasks.append(task)
        return task.id


@pytest.mark.asyncio
async def test_unapproved_manual_source_never_reaches_the_queue():
    dispatcher = Dispatcher()
    policy = SourceAccessPolicy(configured_source_keys=("jobup", "jooble"),
                                allowed_source_keys=())
    service = DiscoveryEnqueueService(
        Searches(a_search_profile()), dispatcher,
        clock=lambda: datetime(2026, 10, 12, tzinfo=UTC), source_policy=policy)

    with pytest.raises(DiscoverySourceAccessDenied):
        await service.enqueue_discovery(USER, SEARCH_PROFILE, trigger="MANUAL")
    assert dispatcher.tasks == []


def test_policy_never_widens_a_users_explicit_source_selection():
    policy = SourceAccessPolicy(configured_source_keys=("jobup", "jooble"),
                                allowed_source_keys=("jooble",))
    assert policy.sources_for(a_search_profile()) == ("jooble",)
    with pytest.raises(DiscoverySourceAccessDenied):
        policy.sources_for(a_search_profile(source_keys=("jobup", "jooble")))


def test_unknown_operator_source_is_a_clear_refusal_not_a_server_crash(monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DISCOVERY_ALLOWED_SOURCES", "unknown_source")
    policy = SourceAccessPolicy.from_env(load_reviewed_boards(
        Path("config/ats_reviewed_boards.yaml")))
    with pytest.raises(DiscoverySourceAccessDenied, match="unknown or disabled"):
        policy.sources_for(a_search_profile())


def test_empty_review_catalog_refuses_ats_even_if_operator_env_allows_it(monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DISCOVERY_ALLOWED_SOURCES", "greenhouse")
    policy = SourceAccessPolicy.from_env(load_reviewed_boards(
        Path("config/ats_reviewed_boards.yaml")))
    with pytest.raises(DiscoverySourceAccessDenied):
        policy.sources_for(a_search_profile(source_keys=("greenhouse",)))


def test_approved_board_is_selected_but_hold_is_not(tmp_path, monkeypatch):
    path = tmp_path / "reviews.yaml"
    from datetime import date

    def board(token: str, decision: str) -> dict[str, object]:
        return {
            "token": token, "company": "Example SA", "country": "CH",
            "provenance_url": "https://example.ch/jobs",
            "conditions_url": "https://example.ch/terms",
            "reviewed_at": date.today().isoformat(),
            "access_decision": decision, "request_budget_per_day": 2,
        }

    path.write_text(yaml.safe_dump({"version": 1, "boards": {
        "greenhouse": [board("reviewed", "approved")],
        "lever": [board("held", "hold")],
    }}), encoding="utf-8")
    monkeypatch.setenv("JOBSEARCH_DISCOVERY_ALLOWED_SOURCES", "greenhouse,lever")
    catalog = load_reviewed_boards(path)
    policy = SourceAccessPolicy.from_env(catalog)
    assert policy.sources_for(a_search_profile(source_keys=("greenhouse",))) == ("greenhouse",)
    with pytest.raises(DiscoverySourceAccessDenied):
        policy.sources_for(a_search_profile(source_keys=("lever",)))

    # Queued work is not permission: a restarted worker reloads the changed catalog.
    path.write_text(yaml.safe_dump({"version": 1, "boards": {
        "greenhouse": [], "lever": [],
    }}), encoding="utf-8")
    assert policy.sources_for(a_search_profile(source_keys=("greenhouse",))) == ("greenhouse",)
    restarted = SourceAccessPolicy.from_env(load_reviewed_boards(path))
    with pytest.raises(DiscoverySourceAccessDenied):
        restarted.sources_for(a_search_profile(source_keys=("greenhouse",)))
