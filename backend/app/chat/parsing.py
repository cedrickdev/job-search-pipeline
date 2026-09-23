"""Parsing a streamed turn into prose and typed proposals — the authority boundary.

This is where "prose has zero authority" becomes mechanical. The model streams one
markdown answer; somewhere inside it there may be a single fenced ```proposal block. This
module pulls that block out *independently of the prose*, parses the JSON, and validates
every action in it against the domain `ChatAction` union. Whatever survives is a typed
proposal the layer can reason about; everything else stays what it always was — text a
human reads, which changes nothing.

Three properties make the parser a boundary rather than a suggestion:

- **The domain union is the only gate.** Each action is validated with
  `CHAT_ACTION_ADAPTER`, so an unknown `kind`, a missing id, an out-of-range radius or an
  invented argument fails to parse into a `ChatAction` and is dropped with a diagnostic —
  it never becomes a half-understood command. The parser is lenient about the *envelope*
  (where the list lives, minor JSON slips) and strict about the *action*.
- **It is total and bounded.** Malformed input yields a `ParsedTurn` with prose and
  diagnostics, never an exception; a turn is capped at `MAX_PROPOSALS_PER_TURN`, and
  everything beyond the cap is dropped with a diagnostic rather than silently executed.
  The one repair it attempts is deterministic (slice to the outermost brackets and retry)
  — never a second model call, which would belong to the service, not the parser.
- **The block never reaches the stored message.** `prose` is the answer with every
  proposal fence removed, which is exactly what `ChatMessage.content` must hold: the block
  is a control payload, not a message a user reads or replays.

Nothing here touches a repository, a provider or a secret — it is a pure function of the
text. Validating that a parsed action may actually run (ownership, state, policy) is the
next stage's job (`backend.app.chat.validators`); parsing only decides what was *said*.
"""
import json
import re
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.chat.prompts import MAX_PROPOSALS_PER_TURN, PROPOSAL_FENCE_TAG
from backend.app.domain.base import NonEmptyStr
from backend.app.domain.chat import CHAT_ACTION_ADAPTER, ChatAction

# The one fenced block the grammar allows the model to emit. Matched case-insensitively and
# tolerant of leading spaces on the info line, because a well-behaved model writes exactly
# ```proposal but the parser must not hinge the whole control path on that exactness. The
# body is captured lazily up to the next closing fence.
_PROPOSAL_BLOCK: Final = re.compile(
    r"```[ \t]*" + re.escape(PROPOSAL_FENCE_TAG) + r"[ \t]*\r?\n(?P<body>.*?)\r?\n?```",
    re.DOTALL | re.IGNORECASE)

# A fence that opened but never closed — a truncated stream, say. Detected only to explain
# in a diagnostic why an obvious block produced no proposals; it is never itself parsed.
_UNTERMINATED_OPEN: Final = re.compile(
    r"```[ \t]*" + re.escape(PROPOSAL_FENCE_TAG) + r"\b", re.IGNORECASE)

# How long a diagnostic may quote the model's own malformed output. A bound, not a guess:
# a diagnostic is for telemetry and the developer, and echoing an unbounded blob helps
# no one.
_MAX_DIAGNOSTIC_LEN: Final[int] = 160


class ParsedProposal(BaseModel):
    """One `{summary, action}` pair recovered from the block and validated.

    A value, not yet a persisted `ChatActionProposal`: it has no id, ordinal or owner —
    the service assigns those when it turns the turn into rows. `summary` is the caption a
    human will read on the confirmation card; `action` is a domain-validated `ChatAction`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: NonEmptyStr
    action: ChatAction


class ParsedTurn(BaseModel):
    """The whole outcome of reading one streamed answer: prose, proposals, diagnostics.

    `prose` is the answer with every proposal fence stripped — what the stored message
    holds. `proposals` are the validated actions, in the order the model listed them, so a
    later `ordinal` is just the index. `errors` are secret-free notes about what was
    dropped and why, kept for telemetry and never executed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prose: str
    proposals: tuple[ParsedProposal, ...] = ()
    errors: tuple[str, ...] = ()


def parse_turn(raw: str) -> ParsedTurn:
    """Split one streamed answer into its prose and its validated typed proposals.

    Total by construction: malformed JSON, an unknown action or too many proposals each
    become a diagnostic on the returned `ParsedTurn`, never an exception. The prose is the
    answer with the proposal block removed; the proposals are whatever validated against
    the domain union, capped at `MAX_PROPOSALS_PER_TURN`.
    """
    errors: list[str] = []
    prose = _PROPOSAL_BLOCK.sub("", raw).strip()

    matches = list(_PROPOSAL_BLOCK.finditer(raw))
    if not matches:
        # No well-formed block. If the model clearly *opened* one, say why it produced
        # nothing rather than leaving the developer to guess at a truncated stream.
        if _UNTERMINATED_OPEN.search(raw):
            errors.append("a proposal block was opened but never closed; ignoring it")
        return ParsedTurn(prose=prose, errors=tuple(errors))
    if len(matches) > 1:
        errors.append(
            f"expected one proposal block, found {len(matches)}; using the first")

    raw_proposals, envelope_error = _proposals_from_block(matches[0].group("body"))
    if envelope_error is not None:
        errors.append(envelope_error)

    proposals: list[ParsedProposal] = []
    for index, entry in enumerate(raw_proposals):
        if index >= MAX_PROPOSALS_PER_TURN:
            errors.append(
                f"more than {MAX_PROPOSALS_PER_TURN} proposals; dropped the rest")
            break
        parsed, error = _parse_one(index, entry)
        if parsed is not None:
            proposals.append(parsed)
        if error is not None:
            errors.append(error)

    return ParsedTurn(prose=prose, proposals=tuple(proposals), errors=tuple(errors))


def _proposals_from_block(body: str) -> tuple[list[object], str | None]:
    """The list of raw proposal entries in the block, plus an envelope diagnostic.

    Strict about the action (that is the domain union's job downstream) but lenient about
    where the list lives, because the three shapes below are the common ways a model
    deviates from the grammar and each is unambiguous. Anything else yields an empty list
    and a diagnostic.
    """
    value, load_error = _load_json_bounded(body)
    if load_error is not None:
        return [], load_error

    # Canonical: {"proposals": [ ... ]}.
    if isinstance(value, dict) and isinstance(value.get("proposals"), list):
        return list(value["proposals"]), None
    # A bare list of entries — the model forgot the envelope key.
    if isinstance(value, list):
        return list(value), None
    # A single proposal object — the model forgot it was meant to be a list.
    if isinstance(value, dict) and "action" in value:
        return [value], None
    return [], "proposal block had no recognizable 'proposals' list"


def _parse_one(index: int, entry: object) -> tuple[ParsedProposal | None, str | None]:
    """Validate one raw entry into a `ParsedProposal`, or explain why it was dropped."""
    if not isinstance(entry, dict):
        return None, f"proposal {index}: expected an object, ignoring it"
    summary = entry.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, f"proposal {index}: missing a non-empty 'summary', ignoring it"
    if "action" not in entry:
        return None, f"proposal {index}: missing an 'action', ignoring it"
    try:
        action = CHAT_ACTION_ADAPTER.validate_python(entry["action"])
    except ValidationError as error:
        return None, f"proposal {index}: {_first_error(error)}"
    return ParsedProposal(summary=summary.strip(), action=action), None


def _load_json_bounded(body: str) -> tuple[object, str | None]:
    """Parse the block body as JSON, with one deterministic repair and no more.

    The repair is the single most common salvage: a model that wraps the JSON in a stray
    prefix or suffix. Slice to the outermost bracket pair and try exactly once — never a
    loop, and never a second model call.
    """
    text = body.strip()
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    sliced = _outermost_bracket_slice(text)
    if sliced is not None:
        try:
            return json.loads(sliced), None
        except json.JSONDecodeError:
            pass
    return None, "proposal block was not valid JSON; ignoring it"


def _outermost_bracket_slice(text: str) -> str | None:
    """The substring from the first opening bracket to the last matching closing one."""
    starts = [pos for pos in (text.find("{"), text.find("[")) if pos != -1]
    ends = [pos for pos in (text.rfind("}"), text.rfind("]")) if pos != -1]
    if not starts or not ends:
        return None
    start, end = min(starts), max(ends)
    return text[start:end + 1] if start < end else None


def _first_error(error: ValidationError) -> str:
    """A short, secret-free summary of the first validation failure on an action."""
    problems = error.errors()
    if not problems:
        return "invalid action, ignoring it"
    first = problems[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid action"))
    summary = f"invalid action at '{location}': {message}" if location else message
    return summary[:_MAX_DIAGNOSTIC_LEN]
