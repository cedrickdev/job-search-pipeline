# tests/test_v2_chat_parsing.py
"""Parsing a streamed turn: the mechanical half of "prose has zero authority".

The model streams one markdown answer with, at most, a single fenced proposal block
inside it. These tests pin what the parser guarantees about that block: the domain union
is the only thing that decides an action is real (an unknown kind, a missing id or an
out-of-range argument is dropped, never half-understood); the parser is total and bounded
(garbage yields diagnostics, not an exception, and a turn is capped); and the block never
survives into the prose the stored message holds. The envelope leniency is checked too,
because a well-behaved model and a slightly-off one must both land on the same typed
proposals or a diagnostic saying why they did not.
"""
import json

from backend.app.chat.parsing import ParsedTurn, parse_turn
from backend.app.chat.prompts import MAX_PROPOSALS_PER_TURN, PROPOSAL_FENCE_TAG
from backend.app.domain.chat import (
    ChatActionKind,
    NavigateAction,
    NavigationTarget,
    SetSearchRadiusAction,
)

OPP = "00000000-0000-4000-8000-000000000021"
SEARCH = "00000000-0000-4000-8000-000000000071"


def _navigate(target: str = "OPPORTUNITIES") -> dict:
    return {"kind": "NAVIGATE", "target": target}


def _fenced(payload: str) -> str:
    """A realistic turn: prose, one fenced proposal block, more prose."""
    return (f"Here is what I suggest.\n\n```{PROPOSAL_FENCE_TAG}\n{payload}\n```\n\n"
            "Confirm when you are ready.")


def _canonical(*actions: dict) -> str:
    proposals = [{"summary": f"do thing {i}", "action": action}
                 for i, action in enumerate(actions)]
    return json.dumps({"proposals": proposals})


# --- the happy path: a canonical block becomes typed proposals -------------

def test_prose_with_no_block_yields_prose_and_no_proposals():
    turn = parse_turn("You have three open applications. Want me to prepare one?")
    assert isinstance(turn, ParsedTurn)
    assert turn.proposals == ()
    assert turn.errors == ()
    assert "three open applications" in turn.prose


def test_a_canonical_block_parses_into_typed_proposals():
    turn = parse_turn(_fenced(_canonical(_navigate("APPLICATIONS"))))
    assert len(turn.proposals) == 1
    proposal = turn.proposals[0]
    assert isinstance(proposal.action, NavigateAction)
    assert proposal.action.target is NavigationTarget.APPLICATIONS
    assert proposal.summary == "do thing 0"
    assert turn.errors == ()


def test_the_proposal_block_never_survives_into_the_prose():
    turn = parse_turn(_fenced(_canonical(_navigate())))
    assert PROPOSAL_FENCE_TAG not in turn.prose
    assert "kind" not in turn.prose
    assert "Here is what I suggest." in turn.prose
    assert "Confirm when you are ready." in turn.prose


def test_proposals_keep_their_order():
    turn = parse_turn(_fenced(_canonical(
        _navigate("OPPORTUNITIES"), _navigate("DOCUMENTS"))))
    assert [p.action.target for p in turn.proposals] == [  # type: ignore[union-attr]
        NavigationTarget.OPPORTUNITIES, NavigationTarget.DOCUMENTS]


# --- the domain union is the only gate -------------------------------------

def test_an_unknown_action_kind_is_dropped_with_a_diagnostic():
    turn = parse_turn(_fenced(_canonical({"kind": "DELETE_EVERYTHING"})))
    assert turn.proposals == ()
    assert len(turn.errors) == 1
    assert "proposal 0" in turn.errors[0]


def test_an_out_of_range_argument_is_dropped():
    turn = parse_turn(_fenced(_canonical(
        {"kind": "SET_SEARCH_RADIUS", "search_profile_id": SEARCH,
         "radius_km": 99999})))
    assert turn.proposals == ()
    assert len(turn.errors) == 1


def test_a_valid_argument_at_the_boundary_is_kept():
    turn = parse_turn(_fenced(_canonical(
        {"kind": "SET_SEARCH_RADIUS", "search_profile_id": SEARCH,
         "radius_km": 500})))
    assert len(turn.proposals) == 1
    assert isinstance(turn.proposals[0].action, SetSearchRadiusAction)


def test_a_proposal_missing_a_summary_is_dropped():
    body = json.dumps({"proposals": [{"action": _navigate()}]})
    turn = parse_turn(_fenced(body))
    assert turn.proposals == ()
    assert any("summary" in error for error in turn.errors)


def test_a_proposal_missing_an_action_is_dropped():
    body = json.dumps({"proposals": [{"summary": "no action here"}]})
    turn = parse_turn(_fenced(body))
    assert turn.proposals == ()
    assert any("action" in error for error in turn.errors)


# --- total and bounded ------------------------------------------------------

def test_more_than_the_cap_is_dropped_with_a_diagnostic():
    actions = [_navigate() for _ in range(MAX_PROPOSALS_PER_TURN + 3)]
    turn = parse_turn(_fenced(_canonical(*actions)))
    assert len(turn.proposals) == MAX_PROPOSALS_PER_TURN
    assert any("more than" in error for error in turn.errors)


def test_an_unterminated_fence_is_reported_and_ignored():
    raw = f"Sure.\n\n```{PROPOSAL_FENCE_TAG}\n{{\"proposals\": [" + "\n(truncated)"
    turn = parse_turn(raw)
    assert turn.proposals == ()
    assert any("never closed" in error for error in turn.errors)


def test_two_blocks_use_the_first_and_diagnose():
    first = _fenced(_canonical(_navigate("OPPORTUNITIES")))
    second = f"```{PROPOSAL_FENCE_TAG}\n{_canonical(_navigate('SETTINGS'))}\n```"
    turn = parse_turn(first + "\n\n" + second)
    assert len(turn.proposals) == 1
    assert turn.proposals[0].action.target is NavigationTarget.OPPORTUNITIES  # type: ignore[union-attr]
    assert any("found 2" in error for error in turn.errors)


def test_totality_never_raises_on_garbage():
    for raw in ("", "```proposal\nnot json at all\n```",
                _fenced("{}"), _fenced("[1, 2, 3]"),
                _fenced('{"proposals": "not a list"}')):
        turn = parse_turn(raw)
        assert isinstance(turn, ParsedTurn)


# --- envelope leniency ------------------------------------------------------

def test_a_bare_list_envelope_is_accepted():
    body = json.dumps([{"summary": "go", "action": _navigate()}])
    turn = parse_turn(_fenced(body))
    assert len(turn.proposals) == 1


def test_a_single_object_envelope_is_accepted():
    body = json.dumps({"summary": "go", "action": _navigate()})
    turn = parse_turn(_fenced(body))
    assert len(turn.proposals) == 1


def test_json_wrapped_in_stray_text_is_repaired():
    inner = _canonical(_navigate("MATCHES"))
    turn = parse_turn(_fenced(f"Certainly! {inner} — hope that helps"))
    assert len(turn.proposals) == 1
    assert turn.proposals[0].action.target is NavigationTarget.MATCHES  # type: ignore[union-attr]


def test_the_action_kind_is_the_domain_enum():
    turn = parse_turn(_fenced(_canonical(_navigate())))
    assert turn.proposals[0].action.kind is ChatActionKind.NAVIGATE
