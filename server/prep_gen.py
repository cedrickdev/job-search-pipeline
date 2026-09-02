"""Interview-prep generation: prompt builder + reply parser (spec §5.4 generate).

The copilot is asked to return ONE JSON object with three string arrays. The
reply runs through the same §6.2 mandate gate as chat (via chat.collect_turn);
this module only shapes the request and extracts the structured fields.
"""
import json
import re

_PREP_INSTRUCTIONS = (
    "You are preparing a candidate for an interview at the company below. "
    "Return ONE fenced ```json block containing an object with exactly three keys, "
    "each a JSON array of short strings:\n"
    '  "likely_questions"  — interview questions this company is likely to ask,\n'
    '  "company_research"   — concrete, verifiable facts worth knowing,\n'
    '  "talking_points"     — strengths the candidate should raise.\n'
    "Ground every item in the provided context; do not invent numbers or "
    "technologies that are not present there. "
    "Never reveal a client name the candidate flagged as confidential: use a "
    "generic alias such as 'a large retail client'. "
    "Always write 'Generative AI', never 'GenAI'."
)

_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

_KEYS = ("likely_questions", "company_research", "talking_points")


def build_prep_prompt(context: str | dict) -> str:
    body = context if isinstance(context, str) else json.dumps(context, indent=2, default=str)
    return _PREP_INSTRUCTIONS + "\n\nCONTEXT:\n" + body


def _normalize(value: object) -> list[str]:
    """Coerce one field into a list of plain strings. company_research items may
    arrive as {point, verify} dicts (spec schema); flatten them to their point."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("point"), str):
            out.append(item["point"])
    return out


def parse_prep_reply(text: str) -> dict:
    """Extract the three prep arrays from a copilot reply. Tolerant: a fenced
    ```json block is preferred, then a bare object; anything unparseable yields
    three empty lists (the route then reports an empty, unverified draft)."""
    empty = {k: [] for k in _KEYS}
    match = _JSON_FENCE_RE.search(text) or _BARE_OBJ_RE.search(text)
    if not match:
        return empty
    raw = match.group(1) if match.re is _JSON_FENCE_RE else match.group(0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return empty
    if not isinstance(obj, dict):
        return empty
    return {k: _normalize(obj.get(k)) for k in _KEYS}
