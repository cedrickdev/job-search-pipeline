"""Tests for pipeline.gap_analysis — JD keyword coverage scoring.

Hermetic: every test passes its own base CV and term universe. These used to
run against the operator's own cv/base_cv.yaml and cv/glossary.yaml, so
re-onboarding a new profile silently rewrote what they asserted.
"""
import pytest

from pipeline.gap_analysis import GREEN_THRESHOLD, analyse

TERMS = ["Python", "SQL", "Git", "Adobe Premiere", "Blender", "encaissement",
         "service en salle"]


@pytest.fixture
def base():
    """Base library covering Python, SQL, Git and Adobe Premiere, not Blender."""
    return {
        "summary": {
            "en": "Media engineering student who versions every project in Git.",
            "fr": "Étudiant en ingénierie des médias qui versionne tout dans Git.",
        },
        "experience": [{
            "id": "acme",
            "bullets": [
                {"id": "acme-etl", "priority": 1,
                 "en": "Built import scripts in Python against a SQL database.",
                 "fr": "Développé des scripts d'import en Python sur une base SQL."},
                {"id": "acme-video", "priority": 2,
                 "en": "Cut the club promo video in Adobe Premiere.",
                 "fr": "Monté la vidéo promo du club dans Adobe Premiere."},
            ],
        }],
        "skills": [{"group": {"en": "Tech", "fr": "Tech"},
                    "items": ["Python", "SQL", "Git"]}],
    }


def test_analyse_empty_jd_returns_full_coverage(base):
    report = analyse("", base, TERMS)
    assert report.coverage_score == 1.0
    assert report.risk_tier == "GREEN"
    assert report.required_keywords == []
    assert report.missing_keywords == []


def test_analyse_jd_with_known_terms_reports_matched(base):
    report = analyse("We need expertise in Python and SQL.", base, TERMS)
    assert "Python" in report.required_keywords
    assert "SQL" in report.required_keywords
    assert "Python" in report.matched_keywords
    assert "SQL" in report.matched_keywords


def test_analyse_jd_with_all_known_terms_is_green(base):
    report = analyse("We need Python, SQL, and Git experience.", base, TERMS)
    assert report.risk_tier == "GREEN"
    assert report.coverage_score >= GREEN_THRESHOLD


def test_analyse_jd_coverage_score_calculation(base):
    report = analyse("Python SQL Blender", base, TERMS)
    assert "Python" in report.matched_keywords
    assert "SQL" in report.matched_keywords
    assert "Blender" in report.missing_keywords
    assert report.coverage_score < 1.0


def test_risk_tier_green_at_threshold(base):
    report = analyse("Python", base, TERMS)
    assert report.coverage_score >= GREEN_THRESHOLD
    assert report.risk_tier == "GREEN"


def test_risk_tier_red_when_all_missing(base):
    report = analyse("Blender experience required", base, TERMS)
    assert report.risk_tier == "RED"
    assert report.coverage_score == 0.0
    assert report.missing_keywords == report.required_keywords


def test_multi_word_terms_are_matched(base):
    report = analyse("Proficiency with Adobe Premiere required.", base, TERMS)
    assert "Adobe Premiere" in report.matched_keywords


def test_term_found_only_in_skills_counts_as_matched(base):
    """Git appears in skills and the summary, never in a bullet."""
    report = analyse("Large-scale Git processing.", base, TERMS)
    assert "Git" in report.matched_keywords


def test_base_cv_skills_join_the_term_universe(base):
    """A skill the user claims is detectable even if the taxonomy omits it."""
    base["skills"][0]["items"].append("Figma")
    report = analyse("Figma platform experience.", base, terms=["Blender"])
    assert "Figma" in report.required_keywords
    assert "Figma" in report.matched_keywords


def test_base_corpus_terms_lists_what_the_library_covers(base):
    report = analyse("", base, TERMS)
    assert set(report.base_corpus_terms) == {"Python", "SQL", "Git",
                                            "Adobe Premiere"}


def test_missing_keywords_not_in_matched(base):
    report = analyse("Python Blender", base, TERMS)
    for kw in report.missing_keywords:
        assert kw not in report.matched_keywords


def test_matched_plus_missing_equals_required(base):
    report = analyse("Python SQL Git Blender", base, TERMS)
    assert (len(report.matched_keywords) + len(report.missing_keywords)
            == len(report.required_keywords))


def test_analyse_defaults_to_the_live_profile():
    """Smoke test on the default wiring (loaded base CV + cv/keywords.yaml):
    the term universe must not be empty, which is what made every report GREEN
    before gap analysis stopped borrowing the translation glossary. conftest.py
    resolves the base CV to tests/fixtures/base_cv.yaml, so this holds in a
    clean clone too."""
    report = analyse("Encaissement en caisse et service en salle.")
    assert report.required_keywords
    assert report.risk_tier in ("GREEN", "YELLOW", "RED")
