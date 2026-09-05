# tests/test_v2_discovery_normalization.py
"""Eleven scraped columns → one `Opportunity`, read through the real Swiss pack.

`build_country_packs().get("CH")` rather than a fixture pack, because §9 asks for a
deterministic Swiss→universal mapping and that is a claim about
`country_packs/ch/*.yaml`, not about a model: a test carrying its own two-term pack
would keep passing while `alternance` went missing from the file operators edit.
Loading it reads YAML and nothing else — §18's no-live-network rule holds.

The order of the file follows the order of the risk. §15 provenance first, because
an `Opportunity` nobody can trace back to a URL is unusable however well it is
classified. Then the pack-driven vocabulary, where the interesting cases are the
ones where two columns disagree and the tie-break decides what the posting *is*.
Then the three traps the V1 inspection turned up, each of which produces a wrong
*fact* about a job rather than a crash:

- the activity rate that lives in V1's `salary` column, which read as a wage makes
  a Migros job pay 80 francs;
- the description that must never be classified, since "apprentissage automatique"
  is machine learning and would file every data role under `APPRENTICESHIP`;
- the scraped wage string that must not become a `SalaryRange`.
"""
from datetime import date, datetime, timedelta

import pytest

from backend.app.discovery.bootstrap import build_country_packs
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import SourceMetadata
from backend.app.discovery.normalization import (
    V1_POSTING_COLUMNS,
    PostingRejected,
    opportunity_from_posting,
)
from backend.app.domain.identifiers import discovered_opportunity_id
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunityType,
    WorkplaceMode,
)
from pipeline.jobs import dedup_hash
from tests.v2_discovery import NOW, a_metadata

CH = build_country_packs().get("CH")

Cap = SourceCapability
Type = OpportunityType
Contract = ContractType
Mode = WorkplaceMode

# Five sources, each carrying exactly one trait a test needs. `PLAIN` is the
# default everywhere: no country to fill in and no capability to unlock, so
# anything it produces came from the pack or from the columns themselves.
PLAIN = a_metadata("wtj")
SWISS = a_metadata("jobup", countries=("CH",))
BORDER = a_metadata("linkedin", countries=("CH", "FR"))
RATES = a_metadata("migros", countries=("CH",),
                   capabilities=frozenset({Cap.STRUCTURED_WORKLOAD}))
APPLY = a_metadata("greenhouse", capabilities=frozenset({Cap.DIRECT_APPLY_URL}))

URL = "https://example.ch/offre/1"
LATER = NOW + timedelta(hours=6)


def a_posting(**columns: object) -> dict[str, object]:
    """A V1 row: the four columns every source fills, plus whatever a test adds."""
    return {"source": "wtj", "company": "Neocraft SA",
            "title": "Developpeur backend", "url": URL} | columns


def normalized(metadata: SourceMetadata = PLAIN, *, fetched_at: datetime = NOW,
               external_id: str | None = None, **columns: object) -> Opportunity:
    """The function under test, with the pack and the clock already decided."""
    return opportunity_from_posting(a_posting(**columns), metadata=metadata, pack=CH,
                                    fetched_at=fetched_at, external_id=external_id)


# --- §15: where a posting came from -------------------------------------------

def test_a_posting_becomes_an_opportunity_that_can_be_traced_back():
    opportunity = normalized()
    assert opportunity.source.source_key == "wtj"
    assert opportunity.source.source_url == URL
    assert opportunity.source.fetched_at == NOW
    assert opportunity.discovered_at == NOW
    assert opportunity.company_name == "Neocraft SA"
    assert opportunity.title == "Developpeur backend"


def test_the_same_posting_met_twice_is_one_opportunity_and_not_two():
    """A sweep runs twice an hour, so a random id per sighting is twelve rows a day."""
    assert normalized().id == normalized(fetched_at=LATER).id
    assert normalized().id == discovered_opportunity_id("wtj", URL)


def test_a_source_publishing_its_own_id_gets_a_stabler_identity_than_a_url():
    """V1 threw it away: `normalize()` has no column for it (§7, §15).

    A URL is a promise a board can break — a slug that grows a tracking parameter
    is a new id and a duplicate row. Greenhouse, Lever and Ashby all number their
    postings, so an adapter that recovers the number passes it here.
    """
    first = normalized(external_id="4001")
    moved = normalized(external_id="4001", url="https://example.ch/jobs/backend-4001")
    assert first.id == moved.id == discovered_opportunity_id("wtj", "4001")
    assert first.source.external_id == "4001"
    assert first.id != normalized().id


def test_the_columns_no_typed_field_claims_survive_word_for_word():
    """§15's snapshot, under the source's own column names and nobody else's."""
    opportunity = normalized(
        location="1003 Lausanne", remote_policy="Hybride", contract_type="CDI",
        salary="CHF 25.-/h", description="Une equipe de six personnes.",
        language="fr", posted_date="2026-02-28")
    assert set(opportunity.source.raw) == {"url", "location", "remote_policy",
                                          "contract_type", "salary", "language",
                                          "posted_date"}
    assert opportunity.source.raw["salary"] == "CHF 25.-/h"
    # The four a typed field already holds verbatim would only be duplicated here.
    assert not {"source", "company", "title", "description"} & set(
        opportunity.source.raw)


def test_a_column_no_v1_source_produces_is_not_carried_into_the_snapshot():
    """`V1_POSTING_COLUMNS` is a whitelist: a stray key is an upstream bug, not data."""
    opportunity = normalized(ats_id="4001", scraped_html="<div>offre</div>")
    assert set(opportunity.source.raw) <= set(V1_POSTING_COLUMNS)
    assert "ats_id" not in opportunity.source.raw


def test_a_blank_column_is_absent_rather_than_stored_empty():
    """A source that scraped nothing said nothing; `""` would look like an answer."""
    opportunity = normalized(location="   ", salary="", posted_date=None)
    assert opportunity.source.raw == {"url": URL}
    assert opportunity.location is None


def test_the_fingerprint_is_v1s_own_hash_so_the_first_sweep_re_inserts_nothing():
    """Phase 2 copied `jobs.dedup_hash` across; a different hash duplicates every row.

    Recomputed in `backend/app/compat/v1_jobs.py` rather than imported, because
    `backend` must not depend on `pipeline` — so the two agreeing is a test, and
    this is the one that checks it on a live sweep's output.
    """
    assert normalized().dedup_fingerprint == dedup_hash("Neocraft SA",
                                                        "Developpeur backend")


def test_two_boards_publishing_one_vacancy_agree_on_the_posting_not_on_the_row():
    """Two questions, two answers: `id` is per-source, the fingerprint is not.

    The orchestrator deliberately does not deduplicate across sources (§13), so
    both rows reach a consumer and the fingerprint is what lets it collapse them.
    """
    jobup = normalized(SWISS, url="https://jobup.ch/offre/9")
    wtj = normalized(PLAIN, url="https://wtj.example/jobs/9")
    assert jobup.dedup_fingerprint == wtj.dedup_fingerprint
    assert jobup.id != wtj.id


# --- what a row has to be able to say -----------------------------------------

@pytest.mark.parametrize("value", [None, "", "   "])
@pytest.mark.parametrize("column", ["company", "title"])
def test_a_row_that_cannot_say_who_is_hiring_for_what_is_refused(column, value):
    """Refused, not repaired: an `Opportunity` with an empty company is worse.

    One such row does not make the source unhealthy — the adapter counts it into
    `postings_skipped` and keeps the other forty (§12's isolation is per source).
    """
    with pytest.raises(PostingRejected) as raised:
        normalized(**{column: value})
    assert raised.value.column == column


def test_a_row_with_neither_an_id_nor_a_url_could_never_be_found_again():
    with pytest.raises(PostingRejected) as raised:
        normalized(url=None)
    assert raised.value.column == "url"


@pytest.mark.parametrize("url", ["/offre/1", "mailto:jobs@example.ch",
                                 "javascript:void(0)", "www.example.ch/offre/1"])
def test_a_scraped_fragment_is_not_a_url(url):
    """`HttpUrlStr` keeps a `mailto:` out of a browser; this keeps it out of an id."""
    with pytest.raises(PostingRejected) as raised:
        normalized(url=url)
    assert raised.value.column == "url"


def test_a_dropped_url_still_survives_in_the_snapshot():
    """The row is usable — it has an id — and the odd link stays auditable (§15)."""
    opportunity = normalized(external_id="4001", url="mailto:jobs@example.ch")
    assert opportunity.source.source_url is None
    assert opportunity.source.raw["url"] == "mailto:jobs@example.ch"


# --- §2, §9: the pack owns the vocabulary -------------------------------------
#
# Every expectation below is a line of `country_packs/ch/*.yaml` and none is a line
# of `normalization.py`. That is §2 as a test rather than a paragraph: `alternance`
# is `WORK_STUDY` because Switzerland says so, and a France pack could answer
# differently without this module changing.

@pytest.mark.parametrize(("title", "expected"), [
    ("Apprentissage d'employe de commerce", Type.APPRENTICESHIP),
    ("Lehre als Kaufmann", Type.APPRENTICESHIP),
    ("Stage en marketing digital", Type.INTERNSHIP),
    ("Praktikum Kommunikation", Type.INTERNSHIP),
    ("Emploi étudiant en logistique", Type.STUDENT_JOB),
    ("Werkstudent Data Engineering", Type.STUDENT_JOB),
    ("Alternance développeur web", Type.WORK_STUDY),
    ("Jeune diplômé ingénieur", Type.GRADUATE),
    ("Vendeuse temporaire", Type.TEMPORARY),
    ("Développeur freelance", Type.FREELANCE),
])
def test_a_swiss_term_in_the_title_names_a_universal_category(title, expected):
    """Ten local words, six universal members, no new enum entry.

    `ALTERNANCE` is not an `OpportunityType` and must not become one: it is a
    French-speaking word for what the domain already calls `WORK_STUDY`. Accents
    are folded on the way in, so a board printing "Développeur" and a YAML key
    written in ASCII still meet.
    """
    assert normalized(title=title).opportunity_type is expected


def test_the_title_says_what_the_role_is_and_the_contract_only_its_shape():
    """A part-time internship is an `INTERNSHIP`, and the column order is why.

    One `classify` call over both strings would answer `PART_TIME`: the pack breaks
    ties by term length and "temps partiel" is longer than "stage". Hence two calls
    in a fixed order rather than one over a joined haystack.
    """
    opportunity = normalized(title="Stage en marketing digital",
                             contract_type="temps partiel")
    assert opportunity.opportunity_type is Type.INTERNSHIP


def test_the_pack_outranks_the_english_token_table():
    """Welcome to the Jungle publishes `contract_type="fulltime"` for internships."""
    assert normalized(title="Stage produit",
                      contract_type="fulltime").opportunity_type is Type.INTERNSHIP


def test_an_ats_token_still_answers_when_no_local_word_appears():
    """The third rung is not dead code: Ashby writes "FullTime" and means it.

    `v1_token` folds the three vendor spellings ("Full-time", "FULL_TIME",
    "FullTime") onto one key, and no Swiss term appears in an English ATS posting
    for the pack to prefer.
    """
    assert normalized(title="Backend engineer",
                      contract_type="FullTime").opportunity_type is Type.FULL_TIME


def test_a_description_is_never_read_for_a_category():
    """Machine learning is "apprentissage automatique", which is the whole trap.

    Any implementation that classified prose would file a data-science posting
    under `APPRENTICESHIP` — a 15-year-old's first contract. The classifier takes no
    description argument at all, so the mistake is not available to make.
    """
    opportunity = normalized(
        title="Ingénieur en science des données",
        description="Apprentissage automatique, vision par ordinateur, et un stage "
                    "de recherche est possible.")
    assert opportunity.opportunity_type is None
    assert opportunity.description.startswith("Apprentissage automatique")


@pytest.mark.parametrize(("contract", "expected"), [
    ("CDI", Contract.PERMANENT),
    ("Festanstellung", Contract.PERMANENT),
    ("CDD 6 mois", Contract.FIXED_TERM),
    ("Travail temporaire", Contract.TEMPORARY_AGENCY),
    ("Personalverleih", Contract.TEMPORARY_AGENCY),
    ("Mandat", Contract.SERVICE_CONTRACT),
])
def test_the_contract_column_states_the_legal_basis(contract, expected):
    """Four legal shapes, in the three languages the Swiss boards publish in."""
    assert normalized(contract_type=contract).contract_type is expected


def test_a_contract_named_in_a_title_is_the_second_place_to_look():
    """Half the jobup titles carry it, and the column is often empty."""
    assert normalized(title="Développeur backend (CDI)").contract_type is \
        Contract.PERMANENT


def test_the_contract_column_outranks_a_title_mentioning_one_in_passing():
    """The mirror of the category order, for the mirror reason.

    A contract column is *about* the legal basis; a title saying "CDI" is doing so
    as an aside, and the two disagreeing means the aside is stale.
    """
    opportunity = normalized(title="Développeur backend (CDI)", contract_type="CDD")
    assert opportunity.contract_type is Contract.FIXED_TERM


def test_one_word_can_name_a_category_without_naming_a_contract():
    """Bare "temporaire" is deliberately absent from the pack's `contract_types`.

    A temporary *position* may be an agency placement, a fixed-term contract or
    an on-call arrangement; the word alone does not say which, and guessing
    `TEMPORARY_AGENCY` would put a legal claim on a posting that made none.
    """
    opportunity = normalized(contract_type="Temporaire")
    assert opportunity.opportunity_type is Type.TEMPORARY
    assert opportunity.contract_type is None


def test_a_staffing_word_answers_both_questions_at_once():
    """`manpower` writes "intérim", which is a category *and* a legal basis.

    Two axes, one word, and both mappings list it — which is what makes the two
    classifiers separate functions over the same column rather than one.
    """
    opportunity = normalized(contract_type="Intérim")
    assert opportunity.opportunity_type is Type.TEMPORARY
    assert opportunity.contract_type is Contract.TEMPORARY_AGENCY


@pytest.mark.parametrize(("column", "value", "expected"), [
    ("remote_policy", "Télétravail", Mode.REMOTE),
    ("remote_policy", "Home office 2 jours par semaine", Mode.REMOTE),
    ("remote_policy", "Hybride", Mode.HYBRID),
    ("remote_policy", "Sur site", Mode.ON_SITE),
    ("title", "Développeur backend en télétravail", Mode.REMOTE),
    ("location", "Remote", Mode.REMOTE),
])
def test_where_the_work_happens_is_read_from_the_three_columns_that_can_say(
        column, value, expected):
    """`pipeline/sources/indeed_ch.py` writes "remote" into the *location* column.

    Hence three fields rather than one — and hence not the description, which is
    the next test.
    """
    assert normalized(**{column: value}).workplace_mode is expected


def test_a_perk_in_the_description_is_not_a_remote_job():
    """A promise of "télétravail après six mois" is a benefit, not a mode.

    Reported as `REMOTE` it would send a candidate looking for remote work to an
    on-site posting, and the sentence that misled them is one a matcher never saw.
    """
    opportunity = normalized(
        description="Possibilité de télétravail après six mois d'essai.")
    assert opportunity.workplace_mode is None


def test_an_english_policy_no_local_term_covers_falls_back_to_the_token_table():
    """To a whole-word matcher "Onsite" is neither "on site" nor "on-site"."""
    assert normalized(remote_policy="Onsite").workplace_mode is Mode.ON_SITE


@pytest.mark.parametrize(("value", "expected"), [
    ("fr", "fr"), ("FR", "fr"), ("Français", "fr"), ("Deutsch", "de"),
    ("italiano", "it"), ("English", "en"),
])
def test_the_posting_language_is_the_one_the_source_declared(value, expected):
    """Four languages, spelled the way each board spells them (§9)."""
    assert normalized(language=value).posting_language == expected


def test_no_language_column_means_no_language_and_never_a_guess():
    """Detecting it from a French title is Phase 9 work with a model behind it."""
    assert normalized(title="Développeur backend", language=None) \
        .posting_language is None


# --- trap one: V1's salary column is not always a salary ----------------------

def test_a_percentage_in_a_title_is_an_activity_rate():
    """Swiss titles state it — "Vendeur/euse 60-80%" — and a title is curated text."""
    workload = normalized(title="Vendeur/euse 60-80%").workload
    assert (workload.min_percent, workload.max_percent) == (60, 80)


def test_a_single_percentage_means_exactly_that_and_not_at_least_that():
    """A posting saying "80%" is a position, not a floor: 100% is not what it asked."""
    workload = normalized(title="Assistant administratif 80%").workload
    assert (workload.min_percent, workload.max_percent) == (80, 80)


def test_a_percentage_in_the_salary_column_is_trusted_only_where_it_belongs():
    """`migros`, `coop` and `jobscout24` put the rate there; nobody else does.

    Read as a wage from those three it makes a Migros job pay 80 francs; ignored
    from them it loses the only workload they state. `STRUCTURED_WORKLOAD` is the
    difference, and both halves are asserted here because either alone is wrong.
    """
    row: dict[str, object] = {"title": "Verkäufer/in", "salary": "80%"}
    trusted = normalized(RATES, **row).workload
    assert (trusted.min_percent, trusted.max_percent) == (80, 80)
    assert normalized(PLAIN, **row).workload is None


@pytest.mark.parametrize("salary", [
    "Taux d'activité : 60 – 80 %", "Taux d’occupation 60-80%", "Pensum 60-80%",
    "Beschäftigungsgrad 60-80%", "Activity rate 60-80%",
])
def test_a_labelled_percentage_is_trusted_from_any_source(salary):
    """The pack's `activity_rate_labels` are what make the column readable at all.

    Three spellings of the same trap: the apostrophe is curly on one board and
    straight on another, the dash is an en dash on a third, and "Pensum" is the
    German-Swiss word. All of them normalize onto one key before matching.
    """
    workload = normalized(PLAIN, title="Verkäufer/in", salary=salary).workload
    assert (workload.min_percent, workload.max_percent) == (60, 80)


def test_a_staff_discount_is_not_a_workload():
    """A description offering "20% de rabais collaborateur" states no activity rate.

    Retail postings say it constantly, which is why the description is not a
    candidate field even for a source that does publish rates in `salary`.
    """
    opportunity = normalized(RATES, title="Verkäufer/in",
                             description="20% de rabais collaborateur sur nos "
                                         "produits.")
    assert opportunity.workload is None


@pytest.mark.parametrize("title", ["Poste à 120%", "Stage non rémunéré 0%",
                                   "Développeur backend"])
def test_an_impossible_or_absent_percentage_leaves_the_workload_unstated(title):
    """`None` is a real answer, and defaulting to 100% would make every job full-time.

    A workload filter would then match postings that never said what they wanted,
    which is worse than matching none of them.
    """
    assert normalized(title=title).workload is None


def test_hours_are_never_derived_from_a_percentage():
    """42 hours is an operator's convention in the pack, not a fact any employer said.

    A consumer that wants hours holds the pack and can convert — a different claim
    from publishing "33.6 h" as something the posting stated.
    """
    workload = normalized(title="Assistant administratif 80%").workload
    assert workload.min_weekly_hours is None and workload.max_weekly_hours is None
    assert CH.metadata.full_time_weekly_hours == 42.0


# --- trap two: nothing is invented --------------------------------------------

def test_a_scraped_wage_never_becomes_a_salary_range():
    """No adapter claims `STRUCTURED_SALARY`, so nothing here may parse prose into one.

    CLAUDE.md forbids fabricating candidate facts; an employer fact invented from
    "CHF 25.-/h" is no better, and a wrong number here would reach a matcher with
    nothing in between to catch it. The string survives for a parser that can do
    better, which is what `raw` is for (§15).
    """
    opportunity = normalized(RATES, salary="CHF 25.-/h")
    assert opportunity.salary is None
    assert opportunity.source.raw["salary"] == "CHF 25.-/h"


# --- where the posting is, at the precision the source actually gave ----------

@pytest.mark.parametrize(("metadata", "expected"), [
    (SWISS, "CH"), (BORDER, None), (PLAIN, None),
], ids=["one country", "two countries", "none declared"])
def test_only_a_source_serving_one_country_can_have_its_country_filled_in(
        metadata, expected):
    """A CH sweep on LinkedIn legitimately returns a Lyon posting.

    So the country comes from the *source* and never from the pack being swept,
    which is the whole reason `sole_country` exists rather than a `countries[0]`.
    """
    location = normalized(metadata, location="Lausanne").location
    assert location.raw == "Lausanne"
    assert location.country == expected


def test_a_single_country_source_locates_a_posting_that_named_no_place():
    """Half a location is still a location, and it is enough for a country filter."""
    assert normalized(SWISS, location=None).location.country == "CH"
    assert normalized(PLAIN, location=None).location is None


def test_the_city_and_the_coordinates_wait_for_phase_seven():
    """Splitting "1003 Lausanne" here would publish a postal code nobody verified.

    Phase 7 owns geocoding, and it re-reads `raw` — which is why the free text is
    kept whole instead of consumed.
    """
    location = normalized(SWISS, location="1003 Lausanne, Suisse").location
    assert location.raw == "1003 Lausanne, Suisse"
    assert location.city is None and location.postal_code is None
    assert location.region is None and location.point is None


# --- what only a capability may claim -----------------------------------------

def test_an_apply_url_is_only_claimed_by_a_source_that_says_so():
    """For a search result the URL is a posting page, and Phase 11 owns the difference.

    An `application_url` is a promise that POSTing there submits an application.
    `greenhouse`, `ashby` and `coop` publish one; a jobup search result does not,
    and its URL is still kept as provenance.
    """
    assert normalized(APPLY).application_url == URL
    plain = normalized(PLAIN)
    assert plain.application_url is None
    assert plain.source.source_url == URL


def test_the_posted_date_is_read_through_v1s_own_parser():
    """V1 writes `YYYY-MM-DD`, so the imported rows and a live sweep agree on the day."""
    assert normalized(posted_date="2026-02-28").posted_at == date(2026, 2, 28)


@pytest.mark.parametrize("posted", ["il y a 3 jours", "vor 2 Tagen", "hier",
                                    "28.02.2026"])
def test_a_relative_date_stays_text_rather_than_becoming_a_wrong_day(posted):
    """A relative date needs the fetch time and a timezone to mean anything.

    Resolving it here would bake this machine's clock into the posting; the string
    stays in `raw`, where a later pass has `fetched_at` beside it (§15).
    """
    opportunity = normalized(posted_date=posted)
    assert opportunity.posted_at is None
    assert opportunity.source.raw["posted_date"] == posted
