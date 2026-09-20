"""The deterministic match engine and its skill vocabulary.

`evaluate_match` is the entry point; `skill_ontology`/`skills` are the pure,
standalone normalizer Phase 10 will feed. Import from here rather than from the
submodules so a later reshuffle stays internal.
"""
from backend.app.matching.engine import MATCH_ENGINE_KEY, evaluate_match
from backend.app.matching.skill_ontology import (
    SKILL_ONTOLOGY,
    CanonicalSkill,
    SkillFamily,
    SkillOntology,
)
from backend.app.matching.skills import NormalizedSkill, normalize_skill

__all__ = [
    "MATCH_ENGINE_KEY",
    "SKILL_ONTOLOGY",
    "CanonicalSkill",
    "NormalizedSkill",
    "SkillFamily",
    "SkillOntology",
    "evaluate_match",
    "normalize_skill",
]
