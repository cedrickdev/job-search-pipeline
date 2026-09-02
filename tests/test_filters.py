from pipeline.filters import matches_scope

CFG = {
    "title_keywords": ["vendeur", "vendeuse", "serveur", "caissier",
                       "employé de commerce", "retail"],
    "exclude_keywords": ["intern", "internship", "stage", "stagiaire",
                         "alternance", "apprenti", "junior", "freelance",
                         "contractor"],
}


def test_matching_title_passes():
    assert matches_scope({"title": "Vendeur polyvalent"}, CFG)


def test_case_insensitive():
    assert matches_scope({"title": "VENDEUSE EN BOULANGERIE"}, CFG)


def test_unrelated_title_rejected():
    assert not matches_scope({"title": "Office Manager"}, CFG)


def test_excluded_keyword_rejected():
    assert not matches_scope({"title": "Retail Intern"}, CFG)
    assert not matches_scope({"title": "Stage - Vendeur"}, CFG)
    assert not matches_scope({"title": "Junior Employé de commerce"}, CFG)


def test_missing_title_rejected():
    assert not matches_scope({"title": None}, CFG)
    assert not matches_scope({}, CFG)
