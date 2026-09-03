# tests/test_base_library.py
"""Sanity gates on the base CV library.

Assertions here must hold for ANY onboarded profile: this file used to pin the
bullet count of the profile the repo shipped with, so it failed the moment a new
user's CV replaced it.

They run against the synthetic tests/fixtures/base_cv.yaml, which conftest.py
substitutes for the operator's gitignored cv/base_cv.yaml, so a clean clone
reports the same verdict as a machine with a real profile onboarded.
"""
from pipeline.cv_render import load_base_cv
from pipeline.glossary import cv_text, glossary_violations, load_glossary
from pipeline.style_check import load_style_rules, style_violations


def test_base_library_is_big_enough_to_select_from():
    """Tailoring picks a relevant subset per offer, so the library needs slack.
    The band is a sanity check (too thin to tailor / duplicated by accident),
    not a spec — the renderer trims by priority whatever is left over."""
    cv = load_base_cv()
    total = sum(len(exp["bullets"]) for exp in cv["experience"])
    assert 8 <= total <= 60


def test_base_bullet_ids_are_unique_and_complete():
    cv = load_base_cv()
    ids = [b["id"] for exp in cv["experience"] for b in exp["bullets"]]
    assert len(ids) == len(set(ids))
    for exp in cv["experience"]:
        for bullet in exp["bullets"]:
            assert bullet["priority"] in (1, 2, 3)
            assert bullet["en"].strip() and bullet["fr"].strip()
            assert bullet["tags"]


def test_base_library_passes_style_and_glossary_gates():
    cv = load_base_cv()
    banned = load_style_rules()
    en_text = cv_text(cv, "en")
    fr_text = cv_text(cv, "fr")
    assert style_violations(en_text, banned) == []
    assert style_violations(fr_text, banned) == []
    assert glossary_violations(en_text, fr_text, load_glossary()) == []
