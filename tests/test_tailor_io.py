"""Tests for pipeline.tailor_io — the five deterministic tailoring gates.

The truth-gate tests run against a synthetic base library (the `base` fixture)
rather than the live cv/base_cv.yaml: they are about `truth_violations` logic,
not about whichever profile is currently onboarded. Only the `generate` tests
touch the real CV, because they render actual PDFs, and they assert on values
derived from that CV instead of hardcoding its content.
"""
import copy
from collections import Counter

import pytest

from pipeline import paths
from pipeline.cv_render import load_base_cv
from pipeline.jobs import insert_job
from pipeline.statuses import create_application
from pipeline.tailor_io import (TailorError, generate, numeric_tokens,
                                truth_violations, unused_bullet_ids)

# Claimable qualifications for the synthetic library. Injected explicitly so the
# gate is tested, not the profile's cv/keywords.yaml.
TERMS = ["Git", "Blender", "HACCP", "German"]


@pytest.fixture
def base():
    """A two-experience library with numbers in bullets and one in the summary."""
    return {
        "contact": {"name": "Test Person", "phone": "+41 00 000 00 00",
                    "email": "test@example.test"},
        "summary": {
            "en": "Sales assistant with 4 years of till experience.",
            "fr": "Vendeur avec 4 ans d'expérience en caisse.",
        },
        "experience": [
            {
                "id": "acme", "company": "Acme", "start": "2023-01", "end": "2025-06",
                "title": {"en": "Sales Assistant", "fr": "Vendeur"},
                "bullets": [
                    {"id": "acme-revenue", "priority": 1, "tags": ["impact"],
                     "en": "Delivered a €2.5M revenue increase from one experiment.",
                     "fr": "Généré 2,5 M€ de revenus grâce à une expérimentation."},
                    {"id": "acme-campaigns", "priority": 2, "tags": ["scale"],
                     "en": "Ran 100 campaigns through the Git pipeline.",
                     "fr": "Piloté 100 campagnes via le pipeline Git."},
                    {"id": "acme-docs", "priority": 3, "tags": ["writing"],
                     "en": "Wrote the runbook the on-call team still uses.",
                     "fr": "Rédigé le runbook encore utilisé par l'équipe d'astreinte."},
                ],
            },
            {
                "id": "globex", "company": "Globex", "start": "2021-09", "end": "2022-12",
                "title": {"en": "Analyst", "fr": "Analyste"},
                "bullets": [
                    {"id": "globex-etl", "priority": 1, "tags": ["data"],
                     "en": "Built ETL jobs feeding the reporting warehouse.",
                     "fr": "Développé des jobs ETL alimentant l'entrepôt de reporting."},
                ],
            },
        ],
        "skills": [{"group": {"en": "Data", "fr": "Data"},
                    "items": ["Git", "SQL"]}],
        "education": [{"school": "HEIG-VD",
                       "degree": {"en": "BSc", "fr": "Bachelor"}}],
        "languages": [{"name": {"en": "French", "fr": "Français"},
                       "level": {"en": "Native", "fr": "Natif"}}],
    }


@pytest.fixture
def tailored(base):
    """A valid tailoring: rewritten summary, bullets copied verbatim."""
    content = copy.deepcopy(base)
    content["summary"]["en"] = ("Data engineer who turns experiments into "
                               "revenue and ships the pipeline behind them.")
    content["summary"]["fr"] = ("Ingénieur data qui transforme les "
                               "expérimentations en revenus et livre le "
                               "pipeline qui va avec.")
    return content


def _violations(base, content):
    return truth_violations(base, content, terms=TERMS)


def test_truth_violations_empty_for_verbatim_selection(base, tailored):
    assert _violations(base, tailored) == []


def test_truth_violations_catch_altered_bullet(base, tailored):
    tailored["experience"][0]["bullets"][0]["en"] = "Delivered €99M in revenue."
    violations = _violations(base, tailored)
    assert any("acme-revenue" in v and "€99M" in v for v in violations)


def test_truth_violations_catch_invented_skill(base, tailored):
    tailored["skills"][0]["items"].append("Quantum Computing")
    assert any("Quantum Computing" in v for v in _violations(base, tailored))


def test_truth_violations_catch_altered_contact(base, tailored):
    tailored["contact"]["phone"] = "+33 6 00 00 00 00"
    assert "contact altered" in _violations(base, tailored)


def test_truth_violations_catch_altered_languages(base, tailored):
    tailored["languages"].append(
        {"name": {"en": "German", "fr": "Allemand"},
         "level": {"en": "Fluent", "fr": "Courant"}})
    assert "languages altered" in _violations(base, tailored)


def test_truth_violations_catch_renamed_skill_group(base, tailored):
    tailored["skills"][0]["group"] = {"en": "AI Leadership", "fr": "Leadership IA"}
    assert any("skill group not in base library" in v
               for v in _violations(base, tailored))


def test_truth_v2_allows_fact_locked_rephrasing(base, tailored):
    bullet = tailored["experience"][0]["bullets"][0]
    bullet["en"] = ("Drove a €2.5M revenue increase by scaling the winning "
                    "variant.")
    bullet["fr"] = ("Généré 2,5 M€ de revenus en déployant la variante "
                    "gagnante.")
    assert _violations(base, tailored) == []


def test_truth_v2_rejects_number_migrated_from_another_bullet(base, tailored):
    bullet = tailored["experience"][0]["bullets"][0]
    bullet["en"] = "Delivered a €2.5M revenue increase across 100 campaigns."
    violations = _violations(base, tailored)
    assert any("acme-revenue" in v and "100" in v for v in violations)


def test_truth_v2_rejects_qualification_absent_from_base_library(base, tailored):
    """Blender is a claimable qualification no base bullet or skill mentions."""
    tailored["experience"][0]["bullets"][2]["en"] = (
        "Wrote the runbook after fine-tuning with Blender.")
    assert any("Blender" in v for v in _violations(base, tailored))


def test_truth_v2_allows_qualification_present_in_skills(base, tailored):
    """Git is only in the skills list, which still licenses using the word."""
    tailored["experience"][1]["bullets"][0]["en"] = (
        "Built ETL jobs in Git feeding the reporting warehouse.")
    assert _violations(base, tailored) == []


def test_truth_v2_rejects_invented_number_in_summary(base, tailored):
    tailored["summary"]["en"] += " I generated €10M in savings."
    violations = _violations(base, tailored)
    assert any("summary" in v and "€10M" in v for v in violations)


def test_truth_v2_allows_base_numbers_in_summary(base, tailored):
    tailored["summary"]["en"] += " Highlights include a €2.5M revenue increase."
    assert _violations(base, tailored) == []


def test_truth_violations_catch_altered_education(base, tailored):
    tailored["education"][0]["school"] = "MIT"
    assert "education altered" in _violations(base, tailored)


def test_truth_violations_catch_altered_title_and_dates(base, tailored):
    tailored["experience"][0]["title"]["en"] = "Principal Data Engineer"
    tailored["experience"][0]["start"] = "2024-01"
    violations = _violations(base, tailored)
    assert any("title altered" in v for v in violations)
    assert any("'start' altered" in v for v in violations)


def test_truth_violations_catch_unknown_bullet_id(base, tailored):
    tailored["experience"][0]["bullets"].append(
        {"id": "made-up", "priority": 2, "en": "x", "fr": "x"})
    assert any("unknown bullet id" in v for v in _violations(base, tailored))


def test_truth_violations_catch_missing_priority(base, tailored):
    del tailored["experience"][0]["bullets"][0]["priority"]
    assert any("priority missing" in v for v in _violations(base, tailored))


def test_unused_bullet_ids_lists_unselected_bullets(base, tailored):
    for exp in tailored["experience"]:
        exp["bullets"] = exp["bullets"][:1]
    unused = unused_bullet_ids(base, tailored)
    assert "acme-revenue" not in unused
    assert unused == ["acme-campaigns", "acme-docs"]


def test_numeric_tokens_extracts_en_formats():
    text = ("Delivered a €2.5M revenue increase, an 80% lift and 4 models "
            "on 100 campaigns")
    assert numeric_tokens(text) == Counter(
        {"€2.5M": 1, "80%": 1, "4": 1, "100": 1})


def test_numeric_tokens_extracts_fr_formats():
    text = "Généré 2,5 M€ de revenus et 80 % de gain sur 100 campagnes"
    assert numeric_tokens(text) == Counter({"2,5M€": 1, "80%": 1, "100": 1})


def test_numeric_tokens_does_not_swallow_words_after_digits():
    assert numeric_tokens("Sold 4 crates of milk on shelf 3") == \
        Counter({"4": 1, "3": 1})


def test_numeric_tokens_is_a_multiset():
    assert numeric_tokens("3 tests of 3 variants") == Counter({"3": 2})


# --- generate(): renders the live profile's CV, so expectations are derived ---

def _seed(conn):
    job_id, _ = insert_job(conn, {"source": "wtj", "company": "Acme Corp",
                                  "title": "Student Job",
                                  "url": "https://x.test/1"})
    app_id = create_application(conn, job_id, source="discovery")
    return job_id, app_id


def _live_tailored():
    """The live base library, unchanged: a verbatim selection always passes."""
    return copy.deepcopy(load_base_cv())


def test_generate_renders_registers_and_transitions(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CV_VERSIONS_DIR", tmp_path)
    job_id, app_id = _seed(conn)
    result = generate(conn, job_id, _live_tailored(),
                      diff_summary="Verbatim selection of the base library.")
    assert result["job_id"] == job_id
    assert len(result["cv_version_ids"]) == 2
    for lang in ("en", "fr"):
        assert (tmp_path / f"acme-corp_{job_id}_{lang}.pdf").exists()
    status = conn.execute("SELECT status FROM applications WHERE id = ?",
                          (app_id,)).fetchone()["status"]
    assert status == "Ready to apply"


def test_generate_rejects_altered_truth(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CV_VERSIONS_DIR", tmp_path)
    job_id, _ = _seed(conn)
    content = _live_tailored()
    content["experience"][0]["company"] = "FancyCo"
    with pytest.raises(TailorError):
        generate(conn, job_id, content, diff_summary="x")
    assert conn.execute("SELECT COUNT(*) FROM cv_versions").fetchone()[0] == 0


def test_generate_rejects_banned_style(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CV_VERSIONS_DIR", tmp_path)
    job_id, _ = _seed(conn)
    content = _live_tailored()
    content["summary"]["en"] = ("Results-driven candidate — I delve into "
                               "seamless synergy.")
    with pytest.raises(TailorError) as exc:
        generate(conn, job_id, content, diff_summary="x")
    assert "delve" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM cv_versions").fetchone()[0] == 0


def test_generate_rejects_sparse_cv_below_fill_floor(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CV_VERSIONS_DIR", tmp_path)
    job_id, _ = _seed(conn)
    content = _live_tailored()
    for exp in content["experience"]:
        exp["bullets"] = exp["bullets"][:1]
    dropped = unused_bullet_ids(load_base_cv(), content)
    with pytest.raises(TailorError) as exc:
        generate(conn, job_id, content, diff_summary="x")
    message = str(exc.value)
    assert "below the 0.92 floor" in message
    # The message must name what is available to add back.
    assert dropped and all(bullet_id in message for bullet_id in dropped)
    assert conn.execute("SELECT COUNT(*) FROM cv_versions").fetchone()[0] == 0
