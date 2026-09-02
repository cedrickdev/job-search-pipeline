from pipeline.style_check import load_style_rules, style_violations


def test_load_style_rules_reads_yaml():
    banned = load_style_rules()
    assert "—" in banned
    assert "delve" in banned


def test_clean_text_passes():
    assert style_violations("I keep the shelves full every shift.", ["—", "delve"]) == []


def test_banned_substring_detected_case_insensitive():
    violations = style_violations(
        "We Delve into data to find synergy.", ["delve", "synergy"])
    assert violations == ["delve", "synergy"]


def test_em_dash_detected():
    assert style_violations("Models — built fast.", ["—"]) == ["—"]
