# tests/test_v2_chat_intent.py
"""The turn-intent classifier: the second server-side wall, and it reads only the user.

Ownership proves an action names *this account's* resource; scope proves it names the
*conversation's* resource; intent — pinned here — proves it corresponds to what the user
actually asked for this turn. These tests hold the classifier to three promises: it maps a
French or English request to the single mutating intent its verb expresses, it fails closed
(no match or two matches → `READ_ONLY`, which authorises nothing), and each intent authorises
only the identically-named kind, so "prepare" can never widen to "submit".

The load-bearing French/English collisions get their own cases: accented `résume` (to
summarise) must not read as `resume`/CV, and the bare noun `candidature` must not read as a
request to *create* an application. The classifier is a pure function of the user's own
words — no repository, no snapshot, no posting text — so every case here is a string in and
an enum out.
"""
import pytest

from backend.app.chat.intent import (
    ALLOWED_CHAT_ACTIONS,
    ChatTurnIntent,
    allowed_action_kinds,
    classify_turn_intent,
)
from backend.app.domain.chat import ChatActionKind

# --- one request, one intent (French + English) -----------------------------


@pytest.mark.parametrize("message, expected", [
    # The accent case: "résume" (summarise) is not "resume" (CV) — it authorises nothing.
    ("Résume-moi cette offre.", ChatTurnIntent.READ_ONLY),
    ("Peux-tu résumer ce poste ?", ChatTurnIntent.READ_ONLY),
    ("Résumé de la description, s'il te plaît.", ChatTurnIntent.READ_ONLY),
    # The plain-e resume / cv / curriculum do mean the document.
    ("Tailor my resume for this posting", ChatTurnIntent.GENERATE_RESUME),
    ("Génère mon CV pour cette offre", ChatTurnIntent.GENERATE_RESUME),
    ("Update my curriculum", ChatTurnIntent.GENERATE_RESUME),
    # Cover letter, in both languages.
    ("Write a cover letter", ChatTurnIntent.GENERATE_COVER_LETTER),
    ("Rédige une lettre de motivation", ChatTurnIntent.GENERATE_COVER_LETTER),
    # Create needs an apply/create verb, never the bare noun.
    ("Postule à cette offre", ChatTurnIntent.CREATE_APPLICATION),
    ("I want to apply here", ChatTurnIntent.CREATE_APPLICATION),
    ("Crée une candidature pour ce poste", ChatTurnIntent.CREATE_APPLICATION),
    # Prepare, in both languages.
    ("Prépare ma candidature.", ChatTurnIntent.PREPARE_APPLICATION),
    ("Prepare my application", ChatTurnIntent.PREPARE_APPLICATION),
    # Approve.
    ("Approuve la candidature", ChatTurnIntent.APPROVE_APPLICATION),
    ("Please approve it", ChatTurnIntent.APPROVE_APPLICATION),
    # Submit — the irreversible boundary — in both languages.
    ("Soumets cette candidature.", ChatTurnIntent.SUBMIT_APPLICATION),
    ("Submit the application", ChatTurnIntent.SUBMIT_APPLICATION),
    ("Envoyer ma candidature", ChatTurnIntent.SUBMIT_APPLICATION),
    # Cancel / withdraw.
    ("Annule ma candidature", ChatTurnIntent.CANCEL_APPLICATION),
    ("Withdraw the application", ChatTurnIntent.CANCEL_APPLICATION),
    # Search-preference edits.
    ("Change le rayon de recherche", ChatTurnIntent.SET_SEARCH_RADIUS),
    ("Set the radius to 50 km", ChatTurnIntent.SET_SEARCH_RADIUS),
    ("Mets à jour les mots-clés", ChatTurnIntent.UPDATE_SEARCH_KEYWORDS),
    ("Update the keywords", ChatTurnIntent.UPDATE_SEARCH_KEYWORDS),
])
def test_a_clear_request_maps_to_its_single_intent(message, expected):
    assert classify_turn_intent(message) is expected


# --- fail closed: no match and multi-match both narrow to READ_ONLY ---------


@pytest.mark.parametrize("message", [
    "Bonjour, comment ça va ?",           # a greeting authorises nothing
    "Où en sont mes candidatures ?",       # the bare noun is not a create verb
    "Quelle est la description du poste ?",  # a question about a posting
    "",                                    # nothing at all
    "   ",                                 # whitespace only
])
def test_an_unrecognised_message_is_read_only(message):
    assert classify_turn_intent(message) is ChatTurnIntent.READ_ONLY


@pytest.mark.parametrize("message", [
    # Two different operations named in one turn — ambiguity narrows to nothing, it never
    # widens the allow-list to the union of both.
    "Prépare et soumets ma candidature",
    "Prepare then submit the application",
    "Annule cette candidature et postule à une autre",
])
def test_a_message_naming_two_operations_is_read_only(message):
    assert classify_turn_intent(message) is ChatTurnIntent.READ_ONLY


# --- the allow-list: each intent authorises only its own kind ---------------


def test_read_only_authorises_no_mutating_action():
    assert allowed_action_kinds(ChatTurnIntent.READ_ONLY) == frozenset()


def test_prepare_never_authorises_submit():
    """The whole point of per-lifecycle intents: a prepare turn cannot drive a submit."""
    allowed = allowed_action_kinds(ChatTurnIntent.PREPARE_APPLICATION)
    assert ChatActionKind.PREPARE_APPLICATION in allowed
    assert ChatActionKind.SUBMIT_APPLICATION not in allowed


@pytest.mark.parametrize("intent", [i for i in ChatTurnIntent
                                    if i is not ChatTurnIntent.READ_ONLY])
def test_every_mutating_intent_authorises_exactly_its_identically_named_kind(intent):
    allowed = allowed_action_kinds(intent)
    assert allowed == frozenset({ChatActionKind[intent.value]})


def test_the_allow_list_covers_every_intent():
    """A new intent added without an allow-list entry would fail here, not at a call site."""
    assert set(ALLOWED_CHAT_ACTIONS) == set(ChatTurnIntent)
