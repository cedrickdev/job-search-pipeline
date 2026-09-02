# tests/test_glossary.py
from pipeline.cv_render import load_base_cv
from pipeline.glossary import cv_text, glossary_violations, load_glossary


def test_violation_detected_when_term_translated():
    terms = ["Motion Design", "sound design"]
    en = "Built Motion Design intros with sound design layers."
    fr_bad = "Réalisé des intros en conception animée avec des couches de son travaillé."
    fr_good = "Réalisé des intros en Motion Design avec des couches de sound design."
    assert glossary_violations(en, fr_bad, terms) == ["Motion Design", "sound design"]
    assert glossary_violations(en, fr_good, terms) == []


def test_base_cv_french_respects_glossary():
    cv = load_base_cv()
    terms = load_glossary()
    violations = glossary_violations(cv_text(cv, "en"), cv_text(cv, "fr"), terms)
    assert violations == []
