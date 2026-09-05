# tests/test_v2_country_packs.py
"""The Swiss pack, and the loader's refusal to accept a broken one (§18).

Two halves, and the split matters. The first loads `country_packs/ch/` — the real
five files an operator edits — because a fake pack can prove the loader works and
cannot prove that *Switzerland* is loadable, which is the acceptance criterion.
The second writes deliberately broken packs into `tmp_path`: §16 requires malformed
configuration to fail loudly with an actionable error, so every rejection is
asserted on its code, not merely on "something raised".

The §2 test is the one to keep if the others were ever cut: `alternance` maps to
`WORK_STUDY` and `ALTERNANCE` is not a member of the universal enum. That is the
whole argument for Country Packs existing, in two assertions.
"""
import yaml
import pytest

from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from country_packs.ch import pack as ch_pack
from country_packs.errors import CountryPackError, CountryPackErrorCode
from country_packs.loader import load_pack
from country_packs.registry import CountryPackRegistry
from tests.v2_discovery import a_pack

CH_DIR = ch_pack.PACK_DIR


@pytest.fixture(scope="module")
def ch():
    """The real Swiss pack, loaded once. Frozen, so sharing it is safe."""
    return ch_pack.load()


def test_ch_pack_loads_and_declares_its_identity(ch):
    assert ch.country == "CH"
    assert ch.metadata.display_name == "Switzerland"
    assert ch.metadata.currency == "CHF"
    assert ch.metadata.default_locale == "fr-CH"
    assert ch.metadata.languages == ("fr", "de", "it", "en")
    assert ch.metadata.timezone == "Europe/Zurich"


def test_ch_pack_converts_an_activity_rate_into_weekly_hours(ch):
    """The one metadata field with teeth: 80% of a 42-hour week (§Workload)."""
    assert ch.metadata.full_time_weekly_hours == 42.0
    assert ch.metadata.weekly_hours_for_percent(80) == pytest.approx(33.6)


def test_ch_pack_enables_the_twelve_v1_sources_in_priority_order(ch):
    """V1's twelve boards, and the order is the pack's judgement, not the code's."""
    assert ch.enabled_source_keys == (
        "jobup", "indeed_ch", "jobscout24", "migros", "coop", "manpower", "wtj",
        "linkedin", "jooble", "greenhouse", "lever", "ashby")
    priorities = [binding.priority for binding in ch.enabled_bindings]
    assert priorities == sorted(p for p in priorities if p is not None)


def test_ch_pack_binds_no_source_outside_switzerland(ch):
    """`indeed` (fr.indeed.com) is absent rather than disabled — see sources.yaml."""
    assert "indeed" not in ch.enabled_source_keys
    assert ch.binding_for("indeed") is None


def test_ch_pack_names_an_env_var_but_never_a_credential(ch):
    """§1: a pack names the variable a source needs, never its value."""
    jooble = ch.binding_for("jooble")
    assert jooble is not None
    assert jooble.config_env_vars == ("JOOBLE_API_KEY",)


# --- §2: universal vocabulary, country terminology ---------------------------

def test_a_country_word_maps_onto_the_universal_enum_without_entering_it(ch):
    """The §2 requirement, in two assertions.

    "alternance" is what a French-speaking board prints; `WORK_STUDY` is what the
    platform reasons about. Adding `ALTERNANCE` to the enum would have been the
    easy fix and would have made every other country's vocabulary a special case.
    """
    assert ch.opportunity_types.classify("Alternance data engineer") \
        is OpportunityType.WORK_STUDY
    assert ch.opportunity_types.classify("Duales Studium Informatik") \
        is OpportunityType.WORK_STUDY
    assert not hasattr(OpportunityType, "ALTERNANCE")
    assert "ALTERNANCE" not in {member.value for member in OpportunityType}


@pytest.mark.parametrize(("text", "expected"), [
    ("Apprentissage employé de commerce", OpportunityType.APPRENTICESHIP),
    ("Lehrstelle Informatik", OpportunityType.APPRENTICESHIP),
    ("Stage marketing 6 mois", OpportunityType.INTERNSHIP),
    ("Praktikum Data Science", OpportunityType.INTERNSHIP),
    ("Job étudiant week-end", OpportunityType.STUDENT_JOB),
    ("Werkstudent Softwareentwicklung", OpportunityType.STUDENT_JOB),
    ("Mission temporaire logistique", OpportunityType.TEMPORARY),
    ("Poste à temps partiel 60%", OpportunityType.PART_TIME),
])
def test_swiss_terms_classify_into_universal_types(ch, text, expected):
    assert ch.opportunity_types.classify(text) is expected


def test_classification_matches_whole_words_only(ch):
    """"lehre" is inside "Lehrer" (teacher) and "stage" inside "stagiaire".

    The first must not classify a teaching post as an apprenticeship. The second
    must still classify, because "stagiaire" is its own key — longest-first is what
    makes both true at once.
    """
    assert ch.opportunity_types.classify("Lehrerin für Mathematik") is None
    assert ch.opportunity_types.classify("Stagiaire en communication") \
        is OpportunityType.INTERNSHIP


def test_an_unrecognized_posting_classifies_as_nothing(ch):
    """`None` means "not classified", which is different from a catch-all member."""
    assert ch.opportunity_types.classify("Ingénieur logiciel senior") is None
    assert ch.opportunity_types.classify(None) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("Télétravail possible", WorkplaceMode.REMOTE),
    ("100% Home Office", WorkplaceMode.REMOTE),
    ("Modèle hybride", WorkplaceMode.HYBRID),
    ("Vor Ort in Zürich", WorkplaceMode.ON_SITE),
    ("Lavoro da remoto", WorkplaceMode.REMOTE),
])
def test_terminology_maps_swiss_workplace_vocabulary(ch, text, expected):
    assert ch.terminology.workplace_mode_for(text) is expected


@pytest.mark.parametrize(("text", "expected"), [
    ("Contrat à durée indéterminée", ContractType.PERMANENT),
    ("CDD 12 mois", ContractType.FIXED_TERM),
    ("Festanstellung", ContractType.PERMANENT),
    ("Travail temporaire", ContractType.TEMPORARY_AGENCY),
    ("Personalverleih", ContractType.TEMPORARY_AGENCY),
])
def test_terminology_maps_swiss_contract_vocabulary(ch, text, expected):
    assert ch.terminology.contract_type_for(text) is expected


def test_a_bare_temporary_is_a_type_and_not_an_agency_contract(ch):
    """Deliberate asymmetry, documented in terminology.yaml.

    "temporaire" says what the engagement is; it does not say that an agency is
    leasing the worker, and asserting that from one word would put a legal claim in
    the database that the posting never made.
    """
    assert ch.terminology.contract_type_for("Poste temporaire") is None
    assert ch.opportunity_types.classify("Poste temporaire") \
        is OpportunityType.TEMPORARY


def test_terminology_recognizes_an_activity_rate_label(ch):
    """Why it exists: three Swiss sources put "80%" in V1's *salary* column."""
    assert ch.terminology.mentions_activity_rate("Taux d'activité: 80%")
    assert ch.terminology.mentions_activity_rate("Pensum 60-80%")
    assert not ch.terminology.mentions_activity_rate("CHF 6000.- par mois")


@pytest.mark.parametrize(("text", "expected"), [
    ("Französisch", "fr"), ("anglais", "en"), ("fr", "fr"), ("italiano", "it"),
])
def test_terminology_maps_language_names_and_iso_codes(ch, text, expected):
    assert ch.terminology.language_for(text) == expected


# --- §16: a malformed pack fails loudly, with a code ------------------------
#
# The valid pack below is France on purpose. It proves the loader is not
# CH-specific, and every rejection test starts from something that loads, so a
# failure is attributable to the one field the test broke.

def _valid_files() -> dict[str, object]:
    return {
        "metadata.yaml": {
            "country": "FR", "display_name": "France", "default_locale": "fr-FR",
            "locales": ["fr-FR"], "currency": "EUR", "languages": ["fr"],
            "timezone": "Europe/Paris", "full_time_weekly_hours": 35.0},
        "sources.yaml": {"sources": [
            {"source_key": "jobup", "priority": 10,
             "expects_capabilities": ["KEYWORD_SEARCH"]}]},
        "opportunity_types.yaml": {"supported": ["FULL_TIME"],
                                   "terms": {"alternance": "WORK_STUDY"}},
        "terminology.yaml": {"workplace_modes": {"teletravail": "REMOTE"}},
        "eligibility.yaml": {"minimum_working_age": 16},
    }


def _write_pack(directory, overrides=None):
    """A loadable pack directory, with the named files replaced or removed.

    A `None` override deletes the file, which is how the missing-file case is
    written without a second helper.
    """
    files = _valid_files() | (overrides or {})
    directory.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        if payload is None:
            continue
        (directory / name).write_text(yaml.safe_dump(payload), encoding="utf-8")
    return directory


def _rejects(directory, overrides, code, *, expected_country=None):
    """Load a broken pack and return the error, having asserted its code."""
    with pytest.raises(CountryPackError) as raised:
        load_pack(_write_pack(directory, overrides),
                  expected_country=expected_country)
    assert raised.value.code is code
    return raised.value


def test_a_pack_that_is_not_switzerland_also_loads(tmp_path):
    """The loader is not the CH pack in disguise (acceptance criterion 8)."""
    pack = load_pack(_write_pack(tmp_path / "fr"), expected_country="FR")
    assert pack.country == "FR"
    assert pack.metadata.currency == "EUR"
    assert pack.enabled_source_keys == ("jobup",)
    assert pack.opportunity_types.classify("Alternance") is OpportunityType.WORK_STUDY


def test_a_missing_file_names_the_file(tmp_path):
    error = _rejects(tmp_path / "p", {"terminology.yaml": None},
                     CountryPackErrorCode.COUNTRY_PACK_FILE_MISSING)
    assert "terminology.yaml" in str(error)


def test_yaml_that_is_not_a_mapping_is_refused(tmp_path):
    _rejects(tmp_path / "p", {"metadata.yaml": ["country: FR"]},
             CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML)


def test_a_sources_key_that_is_not_a_list_is_refused(tmp_path):
    _rejects(tmp_path / "p", {"sources.yaml": {"sources": "jobup"}},
             CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML)


def test_a_country_code_that_is_not_iso_3166_is_refused(tmp_path):
    _rejects(tmp_path / "p",
             {"metadata.yaml": _valid_files()["metadata.yaml"] | {"country": "CHE"}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_COUNTRY)


def test_a_directory_and_its_metadata_must_agree_on_the_country(tmp_path):
    """The mismatch whose symptom — "Swiss searches find nothing" — is far away."""
    error = _rejects(tmp_path / "ch", {}, expected_country="CH",
                     code=CountryPackErrorCode.COUNTRY_PACK_INVALID_COUNTRY)
    assert "FR" in str(error)


def test_a_locale_outside_the_declared_languages_is_refused(tmp_path):
    """A pack that declares de-CH without de would describe a posting it cannot read."""
    _rejects(tmp_path / "p",
             {"metadata.yaml": _valid_files()["metadata.yaml"]
              | {"locales": ["fr-FR", "de-DE"]}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD)


def test_a_term_mapping_to_a_non_universal_type_is_refused(tmp_path):
    """§2, enforced by the loader: the enum is not extended by a YAML file."""
    _rejects(tmp_path / "p",
             {"opportunity_types.yaml": {"terms": {"alternance": "ALTERNANCE"}}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_OPPORTUNITY_TYPE)


def test_an_unnormalized_term_key_is_refused(tmp_path):
    """"Alternance" would never match a normalized posting, and would fail silently."""
    _rejects(tmp_path / "p",
             {"opportunity_types.yaml": {"terms": {"Alternance": "WORK_STUDY"}}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_OPPORTUNITY_TYPE)


def test_an_unnormalized_terminology_key_is_refused(tmp_path):
    _rejects(tmp_path / "p",
             {"terminology.yaml": {"workplace_modes": {"Télétravail": "REMOTE"}}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_TERM_MAPPING)


def test_a_capability_that_does_not_exist_is_refused(tmp_path):
    """A typo in `expects_capabilities` must not become a silent expectation."""
    _rejects(tmp_path / "p",
             {"sources.yaml": {"sources": [{"source_key": "jobup",
                                            "expects_capabilities": ["TELEPATHY"]}]}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_CAPABILITY)


def test_two_bindings_for_one_source_are_refused(tmp_path):
    """Two entries for one board means one of them silently loses."""
    _rejects(tmp_path / "p",
             {"sources.yaml": {"sources": [{"source_key": "jobup"},
                                           {"source_key": "jobup",
                                            "enabled": False}]}},
             CountryPackErrorCode.COUNTRY_PACK_DUPLICATE_SOURCE)


def test_a_credential_value_cannot_be_written_into_a_pack(tmp_path):
    """§1: no credentials in a pack, enforced by `EnvVarName`'s own shape.

    `config_env_vars` accepts `^[A-Z][A-Z0-9_]*$`, which an API key does not match.
    The rule is therefore not reviewable-in-principle, it is unwritable.
    """
    _rejects(tmp_path / "p",
             {"sources.yaml": {"sources": [
                 {"source_key": "jooble",
                  "config_env_vars": ["b7f3c1e9-secret-value"]}]}},
             CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD)


# --- registration ------------------------------------------------------------

def test_a_country_has_exactly_one_pack():
    registry = CountryPackRegistry((a_pack("CH"),))
    with pytest.raises(CountryPackError) as raised:
        registry.register(a_pack("CH"))
    assert raised.value.code is CountryPackErrorCode.COUNTRY_PACK_DUPLICATE_PACK
    assert len(registry) == 1


def test_an_unregistered_country_is_a_named_error_not_an_empty_pack():
    """Returning an empty pack would report that Switzerland has no jobs."""
    registry = CountryPackRegistry((a_pack("FR"),))
    with pytest.raises(CountryPackError) as raised:
        registry.get("CH")
    assert raised.value.code is CountryPackErrorCode.COUNTRY_PACK_NOT_FOUND
    assert registry.find("CH") is None
    assert registry.countries == ("FR",)
