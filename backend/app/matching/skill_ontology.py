"""A deterministic skill vocabulary: canonical names, aliases and families.

Phase 9 §skills asks for skill normalization that is *deterministic* — a lookup
table, not a fuzzy match — and warns against the specific failure of equating
unrelated technologies. `java` and `javascript` share four letters and nothing
else; an edit-distance matcher that treats them as the same skill turns a backend
engineer into a frontend one. So there is no distance metric here at all: a token
is the skill it is spelled as, one of the aliases an operator has explicitly
listed for a canonical skill, or an unknown token preserved verbatim.

Nothing in Phase 9 populates candidate skills or opportunity requirements, so the
match engine does not consult this module yet. It is built now, and tested now,
because Phase 10's extraction pipeline is where skills arrive, and a normalizer
that only appears alongside the code that feeds it is a normalizer written under
pressure. Keeping it pure and standalone means Phase 10 wires it in rather than
inventing it.
"""
from dataclasses import dataclass
from enum import StrEnum


class SkillFamily(StrEnum):
    """The kind of thing a skill is.

    Coarse on purpose: the family exists so a future matcher can tell "the posting
    wants *a* cloud platform and the candidate has one" from "the posting wants
    this exact tool", not to build a taxonomy nobody maintains. `UNKNOWN` is the
    honest family of a token the ontology does not recognise — it is never guessed.
    """

    PROGRAMMING_LANGUAGE = "PROGRAMMING_LANGUAGE"
    FRAMEWORK = "FRAMEWORK"
    LIBRARY = "LIBRARY"
    DATABASE = "DATABASE"
    CLOUD_PLATFORM = "CLOUD_PLATFORM"
    TOOL = "TOOL"
    OPERATING_SYSTEM = "OPERATING_SYSTEM"
    METHODOLOGY = "METHODOLOGY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CanonicalSkill:
    """One recognised skill: its canonical spelling, family and known aliases.

    `canonical` is stored lower-cased, which is also how an unknown token is kept,
    so the two compare without a special case. `aliases` are the other spellings
    that mean this exact skill — abbreviations (`k8s`), punctuation variants
    (`vue.js`) and version-suffixed names (`python3`) — and are matched
    case-insensitively.
    """

    canonical: str
    family: SkillFamily
    aliases: tuple[str, ...] = ()


# The seed vocabulary. Deliberately small and hand-curated rather than scraped:
# every entry is a claim an operator stands behind, and the cost of the table
# being incomplete (an unknown token kept verbatim) is far lower than the cost of
# it being wrong (two unrelated skills merged). Phase 10 extends it; a Country
# Pack may one day carry its own additions.
_ONTOLOGY: tuple[CanonicalSkill, ...] = (
    CanonicalSkill("python", SkillFamily.PROGRAMMING_LANGUAGE,
                   ("py", "python2", "python3")),
    CanonicalSkill("javascript", SkillFamily.PROGRAMMING_LANGUAGE,
                   ("js", "ecmascript", "node", "nodejs", "node.js")),
    CanonicalSkill("typescript", SkillFamily.PROGRAMMING_LANGUAGE, ("ts",)),
    # Its own entry, and pointedly not an alias of javascript.
    CanonicalSkill("java", SkillFamily.PROGRAMMING_LANGUAGE),
    CanonicalSkill("c#", SkillFamily.PROGRAMMING_LANGUAGE,
                   ("csharp", "c sharp", "dotnet", ".net")),
    CanonicalSkill("c++", SkillFamily.PROGRAMMING_LANGUAGE, ("cpp", "cplusplus")),
    CanonicalSkill("go", SkillFamily.PROGRAMMING_LANGUAGE, ("golang",)),
    CanonicalSkill("rust", SkillFamily.PROGRAMMING_LANGUAGE),
    CanonicalSkill("php", SkillFamily.PROGRAMMING_LANGUAGE),
    CanonicalSkill("sql", SkillFamily.PROGRAMMING_LANGUAGE),
    CanonicalSkill("vue", SkillFamily.FRAMEWORK, ("vue.js", "vuejs", "vue 3")),
    CanonicalSkill("react", SkillFamily.FRAMEWORK, ("react.js", "reactjs")),
    CanonicalSkill("angular", SkillFamily.FRAMEWORK, ("angularjs",)),
    CanonicalSkill("nuxt", SkillFamily.FRAMEWORK, ("nuxt.js", "nuxtjs")),
    CanonicalSkill("django", SkillFamily.FRAMEWORK),
    CanonicalSkill("fastapi", SkillFamily.FRAMEWORK),
    CanonicalSkill("spring boot", SkillFamily.FRAMEWORK, ("springboot", "spring")),
    CanonicalSkill("postgresql", SkillFamily.DATABASE,
                   ("postgres", "psql", "postgres sql")),
    CanonicalSkill("mysql", SkillFamily.DATABASE),
    CanonicalSkill("mongodb", SkillFamily.DATABASE, ("mongo",)),
    CanonicalSkill("redis", SkillFamily.DATABASE),
    CanonicalSkill("aws", SkillFamily.CLOUD_PLATFORM, ("amazon web services",)),
    CanonicalSkill("gcp", SkillFamily.CLOUD_PLATFORM, ("google cloud",
                                                       "google cloud platform")),
    CanonicalSkill("azure", SkillFamily.CLOUD_PLATFORM, ("microsoft azure",)),
    CanonicalSkill("docker", SkillFamily.TOOL),
    CanonicalSkill("kubernetes", SkillFamily.TOOL, ("k8s",)),
    CanonicalSkill("git", SkillFamily.TOOL),
    CanonicalSkill("linux", SkillFamily.OPERATING_SYSTEM),
    CanonicalSkill("agile", SkillFamily.METHODOLOGY, ("scrum", "kanban")),
)


class SkillOntology:
    """The alias table, indexed once for constant-time lookup.

    An instance is immutable in practice: the index is built in `__init__` from
    the entries it is given and never mutated. The module exposes one shared
    instance, `SKILL_ONTOLOGY`; a test or a Country Pack that needs a different
    vocabulary constructs its own with its own entries.
    """

    def __init__(self, entries: tuple[CanonicalSkill, ...] = _ONTOLOGY) -> None:
        self._entries = entries
        index: dict[str, CanonicalSkill] = {}
        for entry in entries:
            for spelling in (entry.canonical, *entry.aliases):
                key = spelling.casefold()
                # A duplicate key would mean two canonical skills claim the same
                # spelling — an ontology bug, not a runtime condition, so it fails
                # loudly at construction rather than silently preferring one.
                if key in index and index[key].canonical != entry.canonical:
                    raise ValueError(
                        f"ontology alias {spelling!r} is claimed by both "
                        f"{index[key].canonical!r} and {entry.canonical!r}")
                index[key] = entry
        self._index = index

    @property
    def entries(self) -> tuple[CanonicalSkill, ...]:
        return self._entries

    def canonical_for(self, token: str) -> CanonicalSkill | None:
        """The recognised skill a token spells, or `None` if the table lacks it.

        Case-insensitive and exact: no prefix, substring or fuzzy match, because
        every one of those is a way to merge skills that should stay apart.
        """
        return self._index.get(token.casefold())


# The shared vocabulary. One instance so the alias table is built once, and so a
# caller says `SKILL_ONTOLOGY` rather than deciding which entries to use.
SKILL_ONTOLOGY = SkillOntology()
