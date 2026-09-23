"""Turning a raw skill token into a normalized, comparable one.

The normalizer is deterministic and rule-based: it lower-cases, collapses
whitespace, splits off a trailing version, and looks the remainder up in
`SkillOntology`. There is no similarity threshold anywhere in it — an unknown
token is returned as itself, never snapped to the nearest known skill, because
"nearest" is exactly how `java` becomes `javascript` (see `skill_ontology`).

Unused by the Phase 9 match engine, which has no skill data to normalize, and
present for the reason the ontology is: Phase 10 extraction is where skills
arrive, and it should find this ready rather than write it in a hurry.
"""
import re
from dataclasses import dataclass

from backend.app.matching.skill_ontology import (
    SKILL_ONTOLOGY,
    SkillFamily,
    SkillOntology,
)

# A trailing version, and only a trailing one preceded by whitespace: "python
# 3.11" and "java 17" carry a version, while "log4j" and "c++" do not — the digit
# is part of the name. An optional "v" absorbs "vue v3".
_TRAILING_VERSION = re.compile(r"^(?P<name>.+?)\s+v?(?P<version>\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class NormalizedSkill:
    """A skill token after normalization.

    `known` records whether the ontology recognised it: an unknown skill is still
    returned (with `family` UNKNOWN and `canonical` its cleaned spelling) so the
    caller keeps the candidate's word rather than dropping it, but it is flagged
    so a matcher can weigh a recognised skill differently from one it cannot place.
    `version`, when present, is kept beside the skill rather than folded into it —
    "python" and "python 3.11" are the same skill at possibly different versions,
    a distinction a version-sensitive requirement (Phase 10) will want.
    """

    canonical: str
    family: SkillFamily
    version: str | None
    known: bool
    raw: str

    def same_skill_as(self, other: "NormalizedSkill") -> bool:
        """Whether two tokens name the same skill, version aside.

        Exact canonical equality — the only comparison this module sanctions. Two
        unknown tokens match when spelled identically; a known and an unknown never
        match unless the unknown happens to equal the canonical spelling.
        """
        return self.canonical == other.canonical


def normalize_skill(raw: str,
                    ontology: SkillOntology = SKILL_ONTOLOGY) -> NormalizedSkill | None:
    """Normalize one raw skill token, or `None` if it carries nothing.

    Blank or whitespace-only input is `None` rather than an empty skill: a matcher
    should never hold a requirement that is the empty string.
    """
    cleaned = " ".join(raw.split())
    if not cleaned:
        return None
    match = _TRAILING_VERSION.match(cleaned)
    if match:
        name, version = match.group("name"), match.group("version")
    else:
        name, version = cleaned, None
    entry = ontology.canonical_for(name)
    if entry is not None:
        return NormalizedSkill(canonical=entry.canonical, family=entry.family,
                               version=version, known=True, raw=raw)
    return NormalizedSkill(canonical=name.casefold(), family=SkillFamily.UNKNOWN,
                           version=version, known=False, raw=raw)
