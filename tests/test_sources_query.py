import json
from pathlib import Path

from pipeline.sources import wtj

FIXTURES = Path(__file__).parent / "fixtures"


def test_wtj_parse_hits():
    hits = json.loads((FIXTURES / "wtj_hits.json").read_text())
    jobs = wtj._parse_hits(hits)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "wtj"
    assert first["company"] == "Acme"
    assert first["title"] == "Senior Sales Assistant"
    assert first["url"] == ("https://www.welcometothejungle.com/fr/companies/"
                            "acme/jobs/senior-data-scientist_paris")
    assert first["location"] == "Lausanne"
    assert first["remote_policy"] == "partial"
    assert first["contract_type"] == "full_time"
    assert first["language"] == "en"
    assert first["posted_date"] == "2026-06-08"
    assert jobs[1]["location"] is None  # no offices listed


def test_wtj_fetch_description_reads_json_ld(monkeypatch):
    page = """<html><head><script type="application/ld+json">
    {"@type": "JobPosting", "description": "<p>Own the <b>stock</b> rotation.</p>"}
    </script></head><body></body></html>"""

    class Resp:
        text = page

    monkeypatch.setattr(wtj, "fetch", lambda url, **kw: Resp())
    assert "Own the" in wtj.fetch_description("https://x.test/job")
    assert "stock" in wtj.fetch_description("https://x.test/job")


def test_wtj_creds_fall_back_to_defaults(monkeypatch):
    from pipeline.http_fetch import FetchError

    def boom(url, **kw):
        raise FetchError("waf challenge")

    monkeypatch.setattr(wtj, "fetch", boom)
    monkeypatch.setattr(wtj, "_creds", None)
    assert wtj._algolia_creds() == (wtj.DEFAULT_APP_ID, wtj.DEFAULT_API_KEY)


def test_linkedin_parse_cards():
    from pipeline.sources import linkedin
    html_text = (FIXTURES / "linkedin_search.html").read_text()
    jobs = linkedin._parse_cards(html_text)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "linkedin"
    assert first["company"] == "Acme"
    assert first["title"] == "Senior Sales Assistant"
    assert first["url"] == ("https://fr.linkedin.com/jobs/view/"
                            "sales-assistant-at-acme-4012")
    assert first["location"] == "Lausanne"
    assert first["posted_date"] == "2026-06-09"


def test_linkedin_parse_cards_empty_page():
    from pipeline.sources import linkedin
    assert linkedin._parse_cards("<html><body></body></html>") == []


def test_indeed_parse_mosaic():
    from pipeline.sources import indeed
    html_text = (FIXTURES / "indeed_search.html").read_text()
    jobs = indeed._parse_mosaic(html_text)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "indeed"
    assert job["company"] == "Acme"
    assert job["title"] == "Senior Sales Assistant"
    assert job["url"] == "https://fr.indeed.com/viewjob?jk=abc123"
    assert job["location"] == "Lausanne (VD)"
    assert job["remote_policy"] == "remote"
    assert job["salary"] == "24 CHF par heure"
    assert "Retail" in job["description"] and "weekend" in job["description"]


def test_indeed_parse_mosaic_missing_blob_returns_empty():
    from pipeline.sources import indeed
    assert indeed._parse_mosaic("<html><body>blocked</body></html>") == []


def test_indeed_ch_parse_mosaic_reuses_indeed_shape():
    from pipeline.sources import indeed_ch
    html_text = (FIXTURES / "indeed_search.html").read_text()
    jobs = indeed_ch._parse_mosaic(html_text)
    assert len(jobs) == 1
    assert jobs[0]["source"] == "indeed_ch"
    assert jobs[0]["url"] == "https://ch-fr.indeed.com/viewjob?jk=abc123"


def test_jobup_parse_init():
    from pipeline.sources import jobup
    html_text = (FIXTURES / "jobup_search.html").read_text()
    jobs = jobup._parse_init(html_text)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "jobup"
    assert first["company"] == "Acme Retail SA"
    assert first["title"] == "Employée de commerce étudiant"
    assert first["location"] == "Yverdon-les-Bains"
    assert first["url"] == ("https://www.jobup.ch/fr/emplois/detail/"
                            "aaaa1111-0000-0000-0000-000000000001/")
    assert first["posted_date"] == "2026-06-24"


def test_jobup_parse_init_missing_blob_returns_empty():
    from pipeline.sources import jobup
    assert jobup._parse_init("<html><body>blocked</body></html>") == []


def test_jooble_parse_jobs():
    import json
    from pipeline.sources import jooble
    payload = json.loads((FIXTURES / "jooble_response.json").read_text())
    jobs = jooble._parse_jobs(payload)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "jooble"
    assert first["company"] == "Acme Retail SA"
    assert first["title"] == "Job étudiant vente"
    assert first["location"] == "Yverdon-les-Bains, Vaud"
    assert first["url"] == "https://jooble.org/jdp/123"
    assert first["posted_date"] == "2026-06-24"
    assert jobs[1]["salary"] is None


def test_migros_parse_listing():
    from pipeline.sources import migros
    html_text = (FIXTURES / "migros_search.html").read_text()
    jobs = migros._parse_listing(html_text)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "migros"
    assert first["company"] == "Société coopérative Migros Vaud"
    assert first["title"] == "Vendeur polyvalent (H/F/D)"
    assert first["salary"] == "30 - 40%"
    assert first["location"] == "1020 Renens"
    assert first["contract_type"] == "Emploi fixe (à durée indéterminée)"
    assert first["url"] == ("https://jobs.migros.ch/fr/nos-entreprises/job/"
                            "societe-cooperative-migros-vaud/vendeur-polyvalent/aaaa1111")


def test_migros_parse_listing_empty_page_returns_empty():
    from pipeline.sources import migros
    assert migros._parse_listing("<html><body></body></html>") == []


def test_coop_parse_jobs_filters_by_canton():
    import json
    from pipeline.sources import coop
    payload = json.loads((FIXTURES / "coop_jobs.json").read_text())
    jobs = coop._parse_jobs(payload, "Vaud")
    assert len(jobs) == 1
    first = jobs[0]
    assert first["source"] == "coop"
    assert first["company"] == "Coop"
    assert first["title"] == "Collaboratrice / Collaborateur Restaurant - Poste pour étudiant·e (f/h/d)"
    assert first["url"] == "https://career2.successfactors.eu/career?company=Coop&career_job_req_id=167010"
    assert first["salary"] == "30%"
    assert "encaissement" in first["description"]


def test_coop_parse_jobs_no_canton_filter_returns_all():
    import json
    from pipeline.sources import coop
    payload = json.loads((FIXTURES / "coop_jobs.json").read_text())
    jobs = coop._parse_jobs(payload, None)
    assert len(jobs) == 2


def test_coop_canton_from_location():
    from pipeline.sources import coop
    assert coop._canton_from_location("Yverdon-les-Bains, Suisse") == "Vaud"
    assert coop._canton_from_location("Vaud, Suisse") == "Vaud"
    assert coop._canton_from_location("Valais, Suisse") == "Valais"


def test_jobscout24_parse_listing():
    from pipeline.sources import jobscout24
    html_text = (FIXTURES / "jobscout24_search.html").read_text()
    jobs = jobscout24._parse_listing(html_text)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "jobscout24"
    assert first["company"] == "Coop"
    assert first["title"] == "Vendeur polyvalent H/F"
    assert first["location"] == "1400 Yverdon-les-Bains"
    assert first["salary"] == "40%"
    assert first["url"] == "https://www.jobscout24.ch/fr/job/aaaa1111/"


def test_jobscout24_parse_listing_empty_page_returns_empty():
    from pipeline.sources import jobscout24
    assert jobscout24._parse_listing("<html><body></body></html>") == []


def test_manpower_parse_listing():
    from pipeline.sources import manpower
    html_text = (FIXTURES / "manpower_search.html").read_text()
    jobs = manpower._parse_listing(html_text)
    assert len(jobs) == 2
    first = jobs[0]
    assert first["source"] == "manpower"
    assert first["company"] == "Manpower"
    assert first["title"] == "Vendeur (H/F)"
    assert first["location"] == "Lausanne"
    assert first["contract_type"] == "temporaire"
    assert first["url"] == "https://www.manpower.ch/fr/job/lausanne/vendeur-h-f/111"


def test_manpower_parse_listing_empty_page_returns_empty():
    from pipeline.sources import manpower
    assert manpower._parse_listing("<html><body></body></html>") == []


def test_jooble_requires_api_key(monkeypatch):
    from pipeline.sources import jooble
    from pipeline.http_fetch import FetchError
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    try:
        jooble.search_jobs("job etudiant", "Vaud")
        assert False, "expected FetchError"
    except FetchError:
        pass
