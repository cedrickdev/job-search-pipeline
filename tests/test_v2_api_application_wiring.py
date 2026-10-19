"""API composition must use the same operator gates as the browser worker."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api import dependencies
from backend.app.application_engine.browser.form_capabilities import FormCapabilityCatalog


def test_api_application_and_queue_share_operator_catalog(monkeypatch) -> None:
    catalog = FormCapabilityCatalog(forms=())
    seen = []

    def load(path, *, today):
        seen.append((path, today))
        return catalog

    monkeypatch.setattr(dependencies, "load_form_capabilities", load)
    session = AsyncSession()
    try:
        service = dependencies.application_service(session)
        queue = dependencies.application_queue_service(
            session, datetime(2026, 10, 13, tzinfo=UTC))
        assert service._form_catalog is catalog
        assert queue._form_catalog is catalog
        assert len(seen) == 2
    finally:
        import asyncio
        asyncio.run(session.close())


def test_morning_run_reports_deployment_live_switch(monkeypatch) -> None:
    monkeypatch.setenv("JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED", "true")
    service = dependencies.morning_run_service(AsyncSession(), object())
    assert service._live_submissions_enabled is True
