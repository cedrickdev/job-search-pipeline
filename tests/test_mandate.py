# tests/test_mandate.py
"""Post-generation mandate gate (spec §6.2): client anonymization, 'Generative AI', truth hooks."""
from server import mandate

FORBIDDEN = ["AcmeRetailSA", "NightBarSarl"]
ALIASES = {"AcmeRetailSA": "a large retail client", "NightBarSarl": "a nightlife venue"}


def test_clean_text_passes():
    r = mandate.check_prose("I ran the till for a large retail client.", forbidden=FORBIDDEN)
    assert r.ok and r.violations == []


def test_forbidden_client_name_flagged():
    r = mandate.check_prose("I worked at AcmeRetailSA on the shop floor.", forbidden=FORBIDDEN)
    assert not r.ok
    assert any("AcmeRetailSA" in v for v in r.violations)


def test_genai_wording_flagged():
    r = mandate.check_prose("Expert in GenAI systems.", forbidden=FORBIDDEN)
    assert not r.ok
    assert "genai_wording" in r.violations


def test_apply_fixes_replaces_genai_and_aliases():
    fixed = mandate.apply_fixes("GenAI work at AcmeRetailSA.", aliases=ALIASES)
    assert "GenAI" not in fixed
    assert "Generative AI" in fixed
    assert "AcmeRetailSA" not in fixed
    assert "a large retail client" in fixed


def test_apply_fixes_then_check_is_clean():
    fixed = mandate.apply_fixes("GenAI for NightBarSarl.", aliases=ALIASES)
    assert mandate.check_prose(fixed, forbidden=FORBIDDEN).ok


def test_load_forbidden_from_config(tmp_path, monkeypatch):
    import json
    from pipeline import paths
    cfg = tmp_path / "mandate.json"
    cfg.write_text(json.dumps({"forbidden": ["SecretCo"], "aliases": {"SecretCo": "a retail client"}}))
    monkeypatch.setattr(paths, "MANDATE_CONFIG", cfg)
    assert mandate.load_forbidden() == ["SecretCo"]
    assert mandate.load_aliases() == {"SecretCo": "a retail client"}


def test_load_forbidden_missing_config_returns_empty(tmp_path, monkeypatch):
    from pipeline import paths
    monkeypatch.setattr(paths, "MANDATE_CONFIG", tmp_path / "nope.json")
    # No forbidden NAMES when config is absent, but anonymization itself must
    # fail CLOSED — see test_check_prose_fails_closed_without_config below.
    assert mandate.load_forbidden() == []


# --- Truth gate (spec §6.2): no invented numbers, no tech-terms absent from corpus ---

def test_truth_gate_flags_invented_number():
    corpus = "Delivered a pipeline processing 12 sources for the analytics team."
    # terms=[] isolates the numeric gate from whatever the real glossary holds.
    r = mandate.check_prose("I boosted revenue by 47% across 12 sources.",
                            forbidden=FORBIDDEN, corpus=corpus, terms=[])
    assert not r.ok
    assert any(v.startswith("invented_number:47") for v in r.violations)
    # 12 appears in the corpus, so it must NOT be flagged.
    assert not any(v.startswith("invented_number:12") for v in r.violations)


def test_truth_gate_allows_numbers_present_in_corpus():
    corpus = "Cut latency by 30% and scaled to 5 regions."
    r = mandate.check_prose("Cut latency 30% across 5 regions.",
                            forbidden=FORBIDDEN, corpus=corpus, terms=[])
    assert r.ok and r.violations == []


def test_truth_gate_flags_unsupported_glossary_term():
    corpus = "Built ETL pipelines in Python for analytics."
    # 'Kubernetes' is a protected glossary term not present in the corpus.
    r = mandate.check_prose("Deep Kubernetes and Python expertise.",
                            forbidden=FORBIDDEN, corpus=corpus,
                            terms=["Python", "Kubernetes"])
    assert not r.ok
    assert "unsupported_term:Kubernetes" in r.violations
    assert "unsupported_term:Python" not in r.violations  # in corpus -> allowed


def test_truth_gate_skipped_without_corpus():
    # Free-form chat (no corpus) must not trip the numeric gate.
    r = mandate.check_prose("Here are 3 suggestions for 2 roles.", forbidden=FORBIDDEN)
    assert r.ok and r.violations == []


def test_check_prose_fails_closed_without_config(tmp_path, monkeypatch):
    # When the redaction config is missing, check_prose with no explicit forbidden
    # list must NOT silently pass arbitrary prose: it reports a config-missing
    # violation so generation paths treat output as unverified (fail closed).
    from pipeline import paths
    monkeypatch.setattr(paths, "MANDATE_CONFIG", tmp_path / "nope.json")
    r = mandate.check_prose("Some drafted prose.")
    assert not r.ok
    assert "anonymization_config_missing" in r.violations
