# tests/test_v2_skills.py
"""`normalize_skill` — the deterministic skill vocabulary Phase 10 will feed.

The phase order asks for skill normalization that is a *lookup*, not a fuzzy
match, and it names the specific failure to avoid: `java` and `javascript` share
four letters and nothing else, and an edit-distance matcher that merges them turns
a backend engineer into a frontend one. These tests pin the four properties that
keep it honest — canonical resolution through explicit aliases, a trailing version
split off rather than folded in, unknown tokens preserved verbatim rather than
snapped to a neighbour, and no distance metric anywhere.

Nothing in Phase 9 consults this yet; it is built and tested now so Phase 10 wires
it in rather than writing it under pressure.
"""
import pytest

from backend.app.matching import (
    CanonicalSkill,
    NormalizedSkill,
    SkillFamily,
    SkillOntology,
    normalize_skill,
)


# --- the marquee rule: no distance metric, ever ------------------------------

def test_java_and_javascript_are_different_skills():
    """The failure the whole module exists to prevent: these must never merge."""
    java = normalize_skill("java")
    javascript = normalize_skill("javascript")
    assert java is not None and javascript is not None
    assert java.canonical == "java"
    assert javascript.canonical == "javascript"
    assert java.canonical != javascript.canonical
    assert not java.same_skill_as(javascript)


def test_an_unknown_token_is_never_snapped_to_a_near_neighbour():
    """A typo stays unknown; it is not resolved to the closest known skill.

    `pythonn` is one edit from `python`. A fuzzy matcher would merge them; this
    normalizer keeps it verbatim and flags it unknown, which is the safe error —
    a skill the matcher cannot place, not one it places wrongly.
    """
    result = normalize_skill("pythonn")
    assert result is not None
    assert result.known is False
    assert result.family is SkillFamily.UNKNOWN
    assert result.canonical == "pythonn"


# --- canonical resolution through explicit aliases ----------------------------

@pytest.mark.parametrize(("raw", "canonical", "family"), [
    ("js", "javascript", SkillFamily.PROGRAMMING_LANGUAGE),
    ("nodejs", "javascript", SkillFamily.PROGRAMMING_LANGUAGE),
    ("py", "python", SkillFamily.PROGRAMMING_LANGUAGE),
    ("k8s", "kubernetes", SkillFamily.TOOL),
    ("postgres", "postgresql", SkillFamily.DATABASE),
    (".net", "c#", SkillFamily.PROGRAMMING_LANGUAGE),
    ("vue.js", "vue", SkillFamily.FRAMEWORK),
    ("golang", "go", SkillFamily.PROGRAMMING_LANGUAGE),
])
def test_aliases_resolve_to_their_canonical_skill(raw, canonical, family):
    result = normalize_skill(raw)
    assert result is not None
    assert result.known is True
    assert result.canonical == canonical
    assert result.family is family


def test_resolution_is_case_and_whitespace_insensitive():
    """Casing and surrounding whitespace are noise; the canonical form is stable."""
    for spelling in ("Python", "  PYTHON  ", "python"):
        result = normalize_skill(spelling)
        assert result is not None
        assert result.canonical == "python"
        assert result.known is True


# --- the trailing version is split off, not folded in -------------------------

def test_a_trailing_version_is_kept_beside_the_skill_not_folded_in():
    """`python 3.11` is `python` at version `3.11` — the same skill, versioned."""
    result = normalize_skill("python 3.11")
    assert result is not None
    assert result.canonical == "python"
    assert result.known is True
    assert result.version == "3.11"


def test_a_leading_v_on_the_version_is_absorbed():
    result = normalize_skill("vue v3")
    assert result is not None
    assert result.canonical == "vue"
    assert result.version == "3"


def test_two_versions_of_one_skill_are_the_same_skill():
    """`same_skill_as` compares the skill, not the version."""
    old = normalize_skill("python 2.7")
    new = normalize_skill("python 3.12")
    assert old is not None and new is not None
    assert old.same_skill_as(new)
    assert old.version != new.version


@pytest.mark.parametrize("raw", ["c++", "c#", "log4j"])
def test_a_digit_that_is_part_of_the_name_is_not_a_version(raw):
    """`c++`, `c#`, `log4j` — the character is the name, not a trailing version."""
    result = normalize_skill(raw)
    assert result is not None
    assert result.version is None
    assert result.canonical.startswith(raw[:1].casefold())


# --- unknown tokens are preserved, not dropped --------------------------------

def test_an_unknown_skill_is_preserved_verbatim_and_flagged():
    result = normalize_skill("Elixir")
    assert result is not None
    assert result.known is False
    assert result.family is SkillFamily.UNKNOWN
    assert result.canonical == "elixir"  # cleaned, casefolded, but kept
    assert result.raw == "Elixir"


def test_an_unknown_skill_keeps_its_version_too():
    result = normalize_skill("elixir 1.15")
    assert result is not None
    assert result.known is False
    assert result.canonical == "elixir"
    assert result.version == "1.15"


def test_two_unknown_tokens_match_only_when_spelled_identically():
    a = normalize_skill("cobol")
    b = normalize_skill("COBOL")
    c = normalize_skill("fortran")
    assert a is not None and b is not None and c is not None
    assert a.same_skill_as(b)
    assert not a.same_skill_as(c)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_token_normalizes_to_none(blank):
    """A matcher must never hold a requirement that is the empty string."""
    assert normalize_skill(blank) is None


# --- determinism and the ontology's own guardrail -----------------------------

def test_normalization_is_deterministic():
    """The same token yields an equal result every time — a lookup, not a sample."""
    first = normalize_skill("Spring Boot v3")
    second = normalize_skill("Spring Boot v3")
    assert first == second
    assert isinstance(first, NormalizedSkill)


def test_an_ontology_with_a_clashing_alias_fails_at_construction():
    """Two canonical skills claiming one spelling is a bug, caught loudly at build."""
    with pytest.raises(ValueError, match="claimed by both"):
        SkillOntology((
            CanonicalSkill("java", SkillFamily.PROGRAMMING_LANGUAGE, ("jvm",)),
            CanonicalSkill("kotlin", SkillFamily.PROGRAMMING_LANGUAGE, ("jvm",)),
        ))
