"""Post-generation compliance gate for Claude-drafted prose (spec §6.2).

Separate from pipeline.style_check (writing style). This enforces project mandates:
- client anonymization (real names must never appear; replace with aliases),
- 'Generative AI' wording (never 'GenAI') on prose paths,
- the truth gate: no invented numbers and no protected glossary tech-term that is
  absent from the supplied reference corpus.

Real client names are loaded from a gitignored config (paths.MANDATE_CONFIG), never
hardcoded in committed source. The gate fails CLOSED: when no explicit forbidden
list is passed and the config is absent, prose is reported as unverified.
"""
import json
import re
from dataclasses import dataclass

from pipeline import glossary, paths

_GENAI_RE = re.compile(r"\bGenAI\b")
# Standalone numeric tokens: integers/decimals, optional thousands commas,
# optional trailing percent or plus (e.g. 47, 12.5, 1,000, 30%, 10+).
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?[%+]?")


@dataclass
class MandateResult:
    ok: bool
    violations: list[str]


def _load_config() -> dict:
    path = paths.MANDATE_CONFIG
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _config_present() -> bool:
    return paths.MANDATE_CONFIG.is_file()


def load_forbidden() -> list[str]:
    return list(_load_config().get("forbidden", []))


def load_aliases() -> dict[str, str]:
    return dict(_load_config().get("aliases", {}))


def _safe_glossary_terms() -> list[str]:
    """Protected CV vocabulary; fail open to [] if the glossary file is absent."""
    try:
        return glossary.load_glossary()
    except Exception:
        return []


def _norm_num(tok: str) -> str:
    return tok.rstrip("%+").replace(",", "")


def apply_fixes(text: str, *, aliases: dict[str, str] | None = None) -> str:
    aliases = aliases if aliases is not None else load_aliases()
    fixed = _GENAI_RE.sub("Generative AI", text)
    for real, alias in aliases.items():
        fixed = re.sub(re.escape(real), alias, fixed, flags=re.IGNORECASE)
    return fixed


def check_prose(
    text: str,
    *,
    forbidden: list[str] | None = None,
    corpus: str | None = None,
    terms: list[str] | None = None,
) -> MandateResult:
    """Compliance gate for generated prose.

    Always enforces client anonymization + 'Generative AI' wording. When `corpus`
    is supplied (e.g. CV text + job description), additionally enforces the truth
    gate: every number in `text` must appear in `corpus`, and no protected
    glossary term may appear in `text` unless it also appears in `corpus`. Pass
    an explicit `terms` to override the glossary (e.g. `[]` to skip the term gate).
    Fails CLOSED: with no explicit `forbidden` and no config file, the result is
    flagged `anonymization_config_missing` so callers treat output as unverified.
    """
    explicit_forbidden = forbidden is not None
    forbidden = forbidden if explicit_forbidden else load_forbidden()
    violations: list[str] = []
    low = text.lower()

    # Fail closed: anonymization cannot be verified without a forbidden source.
    if not explicit_forbidden and not _config_present():
        violations.append("anonymization_config_missing")

    for name in forbidden:
        if name.lower() in low:
            violations.append(f"forbidden_client:{name}")
    if _GENAI_RE.search(text):
        violations.append("genai_wording")

    # Truth gate — only when a reference corpus is supplied.
    if corpus is not None:
        corpus_nums = {_norm_num(t) for t in _NUM_RE.findall(corpus)}
        for tok in _NUM_RE.findall(text):
            if _norm_num(tok) not in corpus_nums:
                violations.append(f"invented_number:{tok}")
        corpus_low = corpus.lower()
        check_terms = terms if terms is not None else _safe_glossary_terms()
        for term in check_terms:
            tl = term.lower()
            if tl in low and tl not in corpus_low:
                violations.append(f"unsupported_term:{term}")

    return MandateResult(ok=not violations, violations=violations)
