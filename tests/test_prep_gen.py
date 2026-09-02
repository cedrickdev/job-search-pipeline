"""Prep-generation prompt builder + reply parser (spec §5.4 generate, §6.2 gate)."""
import json

from server import prep_gen


def test_build_prep_prompt_embeds_job_and_mandates():
    ctx = {"job_id": 7, "company": "Alpha", "title": "Shift Lead",
           "description": "Own the weekend stock rotation."}
    p = prep_gen.build_prep_prompt(ctx)
    assert "Alpha" in p and "Shift Lead" in p
    # The three structured fields the model must return.
    assert "likely_questions" in p and "company_research" in p and "talking_points" in p
    # Standing mandates carried into the prep prompt (defense-in-depth, mirrors chat).
    assert "Generative AI" in p
    # Client anonymization: the instruction is asserted, not the example alias,
    # which is profile-dependent prose.
    assert "Never reveal a client name" in p


def test_parse_prep_reply_from_fenced_json():
    obj = {"likely_questions": ["Why us?", "Tell me about a hard project."],
           "company_research": ["Series B in 2024", "Remote-first"],
           "talking_points": ["Led the ETL rebuild"]}
    text = f"Here is your prep.\n```json\n{json.dumps(obj)}\n```\nGood luck!"
    out = prep_gen.parse_prep_reply(text)
    assert out["likely_questions"] == obj["likely_questions"]
    assert out["company_research"] == obj["company_research"]
    assert out["talking_points"] == obj["talking_points"]


def test_parse_prep_reply_from_bare_object():
    obj = {"likely_questions": ["Q1"], "company_research": ["R1"], "talking_points": ["T1"]}
    out = prep_gen.parse_prep_reply(json.dumps(obj))
    assert out == {"likely_questions": ["Q1"], "company_research": ["R1"], "talking_points": ["T1"]}


def test_parse_prep_reply_normalizes_research_objects_to_strings():
    # Spec schema allows company_research items as {point, verify}; the frontend
    # type is string[]. The parser flattens dict items to their `point` string.
    obj = {"likely_questions": [],
           "company_research": [{"point": "Founded 2015", "verify": True}, "Remote-first"],
           "talking_points": []}
    out = prep_gen.parse_prep_reply(f"```json\n{json.dumps(obj)}\n```")
    assert out["company_research"] == ["Founded 2015", "Remote-first"]


def test_parse_prep_reply_garbage_yields_empty_lists():
    out = prep_gen.parse_prep_reply("Sorry, I could not produce prep right now.")
    assert out == {"likely_questions": [], "company_research": [], "talking_points": []}
