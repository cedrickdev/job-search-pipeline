"""Protected-terms check: technical vocabulary must survive translation.

Two distinct vocabularies live here, and conflating them is a bug:
`load_glossary()` returns the terms that must never be translated in the French
CV, while `load_keywords()` returns the JD keyword taxonomy gap analysis and the
invented-technology gate search for.
"""
import yaml

from pipeline import paths

KEYWORDS_FILE = "keywords.yaml"


def load_glossary() -> list[str]:
    data = yaml.safe_load((paths.CV_DIR / "glossary.yaml").read_text())
    return data["protected_terms"]


def _dedupe(terms: list[str]) -> list[str]:
    """Case-insensitive dedupe, first occurrence wins so order stays stable."""
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def _keywords_data() -> dict:
    path = paths.CV_DIR / KEYWORDS_FILE
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_keywords(categories: list[str] | None = None) -> list[str]:
    """Flattened JD keyword taxonomy from cv/keywords.yaml.

    The file groups terms by category for readability; categories carry no
    meaning downstream except through `categories`, which restricts the result
    to the named groups (used for the claimable-qualification gate). Falls back
    to the protected terms when the file is absent, which keeps a profile
    onboarded before the split working instead of silently matching nothing.
    """
    data = _keywords_data()
    groups = data.get("jd_keywords") or {}
    if not groups:
        return load_glossary()
    if isinstance(groups, list):  # flat list is also accepted
        return _dedupe(list(groups))
    if categories is not None:
        wanted = set(categories)
        groups = {name: terms for name, terms in groups.items() if name in wanted}
    return _dedupe([term for group in groups.values() for term in (group or [])])


def load_hard_skill_terms() -> list[str]:
    """Terms a tailored CV may not introduce unless the base library has them.

    The protected glossary plus the keyword categories flagged as claimable
    qualifications (`hard_skill_categories`). Generic offer vocabulary is left
    out on purpose so honest rephrasing is not rejected as invention.
    """
    categories = _keywords_data().get("hard_skill_categories")
    keywords = load_keywords(categories=categories) if categories else []
    return _dedupe(load_glossary() + keywords)


def cv_text(cv: dict, lang: str) -> str:
    """Concatenate all human-visible text of a CV dict for one language."""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if lang in node and isinstance(node[lang], str):
                parts.append(node[lang])
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            parts.append(node)

    walk(cv)
    return " ".join(parts)


def glossary_violations(en_text: str, fr_text: str, terms: list[str]) -> list[str]:
    """Terms present in the EN version but missing from the FR version."""
    en_low, fr_low = en_text.lower(), fr_text.lower()
    return [t for t in terms if t.lower() in en_low and t.lower() not in fr_low]
