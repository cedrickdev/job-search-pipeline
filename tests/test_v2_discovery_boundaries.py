# tests/test_v2_discovery_boundaries.py
"""Acceptance criteria 2 and 8, read off the import graph rather than trusted.

"Core orchestration has no hard-coded Swiss source list" and "adding another
country needs no core orchestration change" are both statements about *shape*. A
behavioural test cannot see them: a sweep would pass just as well with
`if source_key == "jooble"` in the middle of `_discover`, and the day someone adds
that line the tests that would notice are the ones that read the code.

So these do. The analysis is static (`ast`, `Path.read_text`) and nothing here
imports the modules it inspects, which is what lets it assert absences.

One consequence is worth stating, because it looks like a loophole: the prose in
these modules names sources constantly — `contracts.py` explains `sole_country`
with `ch-fr.indeed.com` and `failures.py` opens on `pipeline/sources/jooble.py`.
That is the *justification* of a rule living beside it, and a comment carries no
behaviour, so the source-key test reads identifiers and live string literals and
skips docstrings. Comments never reach the AST at all.

The layering (§13) it pins:

    capabilities → contracts → {failures, normalization, registry, requests}
                 → orchestrator → bootstrap → [adapters] → pipeline (V1)

`bootstrap` is the only module allowed to know which adapters exist and which
country packs are shipped; the adapters are the only ones allowed to import V1;
`country_packs` reaches no further into the framework than two contract modules.
"""
import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_DIR = REPO_ROOT / "backend" / "app" / "discovery"
ADAPTERS_DIR = DISCOVERY_DIR / "adapters"
PACKS_DIR = REPO_ROOT / "country_packs"
CH_DIR = PACKS_DIR / "ch"

DISCOVERY_PACKAGE = "backend.app.discovery"

# The dependency order the package declares, framework-first. Pinned here so the
# layering is checked edge by edge: every import inside `backend/app/discovery`
# has to point backwards in this tuple, which is what keeps `registry` from
# growing into `orchestrator` and keeps both out of `bootstrap`.
FRAMEWORK_ORDER = ("capabilities", "contracts", "failures", "normalization",
                   "registry", "requests", "orchestrator", "bootstrap")

# Everything but the composition module. These eight files are what "core
# orchestration" means in criterion 2, and none of them may name a source.
CORE_MODULES = tuple(module for module in FRAMEWORK_ORDER if module != "bootstrap")

ADAPTER_MODULES = ("v1_catalog", "v1_sources")

# The thirteen keys `adapters/v1_catalog.py` registers. Spelled out rather than
# imported from the catalog: importing it would make this test pass by construction
# on the day someone renames a source, and the point is that these words do not
# belong in the core whatever the catalog currently says.
SOURCE_KEYS = frozenset({
    "jobup", "indeed", "indeed_ch", "jobscout24", "migros", "coop", "manpower",
    "wtj", "linkedin", "jooble", "greenhouse", "lever", "ashby",
})

# What a Country Pack may reach for: typed contracts and a vocabulary of
# capabilities. §1 forbids executable LLM logic and browser automation in a pack,
# and importing `orchestrator` or an adapter would be the first step to either.
PACK_ALLOWED_FRAMEWORK = frozenset({"capabilities", "contracts"})

# Named so a violation fails with the name of the thing that was reached for. The
# allowlist test beside it is the general rule that catches tomorrow's client.
INFRASTRUCTURE_ROOTS = frozenset({
    "requests", "httpx", "aiohttp", "urllib3", "bs4", "lxml", "selenium",
    "playwright", "anthropic", "openai", "claude_code_sdk", "litellm",
    "sqlite3", "sqlalchemy", "alembic", "psycopg", "asyncpg", "redis",
    "fastapi", "starlette", "uvicorn", "jinja2", "weasyprint", "openpyxl",
    "pipeline", "server", "dashboard", "tests",
})

# §3: the orchestrator must not be able to tell whether a source uses HTTP, HTML,
# Playwright, RSS, an ATS or a career page. `yaml` is on the list because a pack
# file is the loader's business and a board's own configuration is an adapter's.
CORE_ALLOWED_ROOTS = frozenset(sys.stdlib_module_names) | {
    "pydantic", "backend", "country_packs"}

# A pack loads YAML, so `yaml` joins the list — but only for the loader, which one
# test pins separately.
PACK_ALLOWED_ROOTS = CORE_ALLOWED_ROOTS | {"yaml"}

_WORD = re.compile(r"[a-z][a-z0-9_]*")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _prose(tree: ast.Module) -> set[int]:
    """Every string constant that is documentation: a docstring or a bare string.

    Identified by position rather than content — a string that is a statement all
    by itself explains something to a reader and cannot change what runs.
    """
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _executable_words(path: Path) -> set[str]:
    """Every word a module's *code* contains: identifiers and live string literals.

    Comments are absent from an AST, and docstrings are subtracted, so what is left
    is what the interpreter acts on. That is the set a rule about behaviour has to
    be checked against.
    """
    tree = _tree(path)
    prose = _prose(tree)
    words: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Name(id=text) | ast.Attribute(attr=text) | ast.arg(arg=text):
                pass
            case ast.FunctionDef(name=text) | ast.AsyncFunctionDef(name=text) \
                    | ast.ClassDef(name=text):
                pass
            case ast.keyword(arg=str() as text) | ast.alias(name=text):
                pass
            case ast.Constant(value=str() as text) if id(node) not in prose:
                pass
            case _:
                continue
        words |= set(_WORD.findall(text.lower()))
    return words


def _imported_modules(path: Path) -> set[str]:
    """Every module a file imports, by full dotted name."""
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            modules.add(module)
            # `from country_packs import ch` and `from x.y import z` name the same
            # kind of thing; a dependency on a submodule must not hide in the tail.
            modules |= {f"{module}.{alias.name}" for alias in node.names}
    return modules


def _import_roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imported_modules(path)}


def _framework_imports(path: Path) -> set[str]:
    """The `backend.app.discovery` submodules a file imports, by bare name.

    Both spellings count: `from backend.app.discovery import failures` and
    `from backend.app.discovery.registry import SourceRegistry`.
    """
    prefix = f"{DISCOVERY_PACKAGE}."
    return {module[len(prefix):].split(".")[0]
            for module in _imported_modules(path) if module.startswith(prefix)}


def _framework_path(module: str) -> Path:
    return DISCOVERY_DIR / f"{module}.py"


def _python_files(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in directory.rglob("*.py")
                        if "__pycache__" not in path.parts))


PACK_FILES = _python_files(PACKS_DIR)
PACK_IDS = tuple(str(path.relative_to(REPO_ROOT)) for path in PACK_FILES)


# --- criterion 2: no source is named in the core ------------------------------

def test_the_framework_is_the_modules_the_layering_lists():
    """A new module has to be placed in the order, not merely dropped in.

    Without this, `dispatch.py` could appear tomorrow and inherit none of the
    rules below, because every test here is parametrized over the tuple.
    """
    present = tuple(sorted(path.stem for path in DISCOVERY_DIR.glob("*.py")
                           if path.stem != "__init__"))
    assert present == tuple(sorted(FRAMEWORK_ORDER))


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_names_a_single_source_in_its_code(module):
    """Criterion 2, and the one test that would catch `if source_key == "jooble"`.

    The prose in these files names sources on nearly every page — that is where a
    capability claim cites the V1 line behind it. What may not happen is a source
    key reaching an identifier, a comparison or a live string, because that is the
    hard-coded list the phase exists to remove.
    """
    named = _executable_words(_framework_path(module)) & SOURCE_KEYS
    assert named == frozenset(), f"{module} names {sorted(named)} in code"


def test_the_orchestrator_imports_neither_an_adapter_nor_the_composition_module():
    """The sharp form of criterion 2: the sweep cannot reach a concrete source.

    `sweep()` receives a registry and a pack registry. If it could import
    `adapters` it could also fall back to one, and the "adding a source means
    editing the algorithm" shape would be one import away from returning.
    """
    imports = _framework_imports(_framework_path("orchestrator"))
    assert "adapters" not in imports
    assert "bootstrap" not in imports


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_imports_the_composition_module(module):
    """Composition depends on the framework; the framework must not look back."""
    assert "bootstrap" not in _framework_imports(_framework_path(module))


@pytest.mark.parametrize("module", FRAMEWORK_ORDER)
def test_every_import_inside_the_framework_points_backwards(module):
    """An acyclic layering, edge by edge, so a cycle fails with both names."""
    position = FRAMEWORK_ORDER.index(module)
    for imported in _framework_imports(_framework_path(module)) - {"adapters"}:
        assert imported in FRAMEWORK_ORDER, f"{imported} is not placed in the order"
        assert FRAMEWORK_ORDER.index(imported) < position, \
            f"{module} imports {imported}, which comes after it"


@pytest.mark.parametrize("module", CORE_MODULES)
def test_only_the_composition_module_knows_which_adapters_exist(module):
    """§13: registering a source is composition, not orchestration."""
    assert "adapters" not in _framework_imports(_framework_path(module))


def test_the_composition_module_is_where_the_two_halves_meet():
    """Stated positively, so the rules above are not vacuously satisfied.

    If nothing wired the adapters to the registry, every "the core does not import
    an adapter" test would pass and discovery would sweep nothing.
    """
    imports = _framework_imports(_framework_path("bootstrap"))
    assert "adapters" in imports
    assert {"orchestrator", "registry"} <= imports
    assert "country_packs.ch" in _imported_modules(_framework_path("bootstrap"))


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_knows_that_switzerland_is_the_first_country(module):
    """Criterion 8, in the only form a test can check before France exists.

    Adding a country means writing `country_packs/fr/` and one line in
    `bootstrap` — which §8 explicitly allows — and touching none of these eight
    files. A core module importing `country_packs.ch` is what would break that,
    and importing the pack *contracts* is how it is avoided.
    """
    reached = {name.split(".")[1]
               for name in _imported_modules(_framework_path(module))
               if name.startswith("country_packs.")}
    assert reached <= {"contracts", "registry", "errors", "loader"}, \
        f"{module} reaches country_packs.{sorted(reached)}"


# --- §3, §7: V1 lives behind the adapters and nowhere else --------------------

@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_can_tell_how_a_source_fetches_anything(module):
    """§3: HTTP, HTML, Playwright, RSS, an ATS or a career page — all invisible here.

    An allowlist rather than a blocklist, because the import that matters is the
    one nobody has thought of yet: the first `httpx` in `orchestrator.py` would be
    the moment the sweep started knowing how one particular source works.
    """
    roots = _import_roots(_framework_path(module))
    assert roots & INFRASTRUCTURE_ROOTS == frozenset(), \
        f"{module} imports {sorted(roots & INFRASTRUCTURE_ROOTS)}"
    assert roots <= CORE_ALLOWED_ROOTS, \
        f"{module} imports {sorted(roots - CORE_ALLOWED_ROOTS)}"


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_imports_v1(module):
    """§7's seam: V1 is wrapped, and the wrapper is the only thing that sees it.

    `normalization.py` is the interesting case — it interprets V1's eleven columns
    and re-declares their names rather than importing `pipeline.sources._common`,
    which is why the list appears twice in the repository on purpose.
    """
    assert "pipeline" not in _import_roots(_framework_path(module))


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_an_adapter_is_the_one_place_v1_is_imported(module):
    """Stated positively: the strangler seam exists and is exactly here."""
    assert "pipeline" in _import_roots(ADAPTERS_DIR / f"{module}.py")


@pytest.mark.parametrize("module", ADAPTER_MODULES)
def test_an_adapter_never_learns_about_the_sweep_it_runs_in(module):
    """A wrapper answers one request; the registry and the orchestrator are above it.

    An adapter that could reach the registry could record its own health or read a
    sibling's, and §14's isolation guarantee would stop being structural.
    """
    imports = _framework_imports(ADAPTERS_DIR / f"{module}.py")
    assert imports & {"orchestrator", "registry", "bootstrap", "requests"} \
        == frozenset(), f"{module} imports {sorted(imports)}"


def test_the_source_type_is_never_dispatched_on():
    """§4: `SourceType` is descriptive, and nothing may branch on it.

    The moment a sweep reads it, "ATS boards are handled this way" becomes
    expressible and the Protocol stops being the only thing the orchestrator
    knows. `test_v2_source_capabilities.py` pins the enum's members and points
    here for the structural half.

    Two places may name it: `contracts.py`, which defines it, and the catalog,
    which declares one per source.
    """
    for module in CORE_MODULES:
        if module == "contracts":
            continue
        words = _executable_words(_framework_path(module))
        assert "sourcetype" not in words, f"{module} reads SourceType"
        assert "source_type" not in words, f"{module} reads source_type"


# --- §1: a Country Pack is data, and reaches almost nothing -------------------

@pytest.mark.parametrize("path", PACK_FILES, ids=PACK_IDS)
def test_a_country_pack_reaches_no_further_than_two_contract_modules(path):
    """§1: provider-neutral and source-neutral, as an import rule.

    A pack that could import `registry` or an adapter would be able to *do*
    discovery instead of describing a country's part in it — and the first thing to
    follow would be a source-specific workaround inside a country's own file.
    """
    reached = _framework_imports(path)
    assert reached <= PACK_ALLOWED_FRAMEWORK, \
        f"{path.name} imports {sorted(reached - PACK_ALLOWED_FRAMEWORK)}"


@pytest.mark.parametrize("path", PACK_FILES, ids=PACK_IDS)
def test_no_country_pack_module_can_call_an_llm_or_drive_a_browser(path):
    """§1, verbatim: no executable LLM logic and no browser automation in a pack.

    Both are named in the work order because both are tempting: a country's
    terminology looks like something a model could infer, and a career site looks
    like something a pack could scrape. Neither belongs in a data file's loader.
    """
    roots = _import_roots(path)
    assert roots & INFRASTRUCTURE_ROOTS == frozenset(), \
        f"{path.name} imports {sorted(roots & INFRASTRUCTURE_ROOTS)}"
    assert roots <= PACK_ALLOWED_ROOTS, \
        f"{path.name} imports {sorted(roots - PACK_ALLOWED_ROOTS)}"


def test_only_the_loader_parses_yaml():
    """One place reads a file, so one place raises §16's actionable error.

    A pack module that parsed its own YAML would each need its own error handling,
    and the second one written would be the one that swallowed a typo.
    """
    parsers = {path.name for path in PACK_FILES if "yaml" in _import_roots(path)}
    assert parsers == {"loader.py"}


def test_a_country_is_five_data_files_and_one_loader_shim():
    """§1's file list, and criterion 8's real content: a country is data.

    `pack.py` holds a cached `load_pack` call and nothing else. If a country ever
    needed a second module, that would be the moment to ask what executable
    behaviour had leaked into a pack — hence a test rather than a convention.
    """
    assert {path.name for path in CH_DIR.glob("*.py")} == {"__init__.py", "pack.py"}
    assert {path.name for path in CH_DIR.glob("*.yaml")} == {
        "metadata.yaml", "sources.yaml", "opportunity_types.yaml",
        "terminology.yaml", "eligibility.yaml"}


# A token long enough and mixed enough to be a credential rather than a word.
# German compounds reach 25 letters ("beschaeftigungsgrad" is 19), so length alone
# would be a false positive; a digit inside a long unbroken run is what no term in
# any of these files has and what every API key does.
_KEY_SHAPED = re.compile(r"(?=[a-z0-9]*\d)[a-z0-9]{16,}", re.IGNORECASE)

# The keys that may legitimately hold something credential-adjacent — names only.
_ENV_VAR_KEYS = frozenset({"config_env_vars", "credential_env_vars"})
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

YAML_FILES = tuple(sorted(PACKS_DIR.rglob("*.yaml")))
YAML_IDS = tuple(str(path.relative_to(REPO_ROOT)) for path in YAML_FILES)


def _scalars(node: object, key: str = "") -> list[tuple[str, str]]:
    """Every string in a loaded YAML document, paired with the key that holds it."""
    match node:
        case dict():
            return [pair for name, value in node.items()
                    for pair in _scalars(value, str(name))]
        case list():
            return [pair for item in node for pair in _scalars(item, key)]
        case str():
            return [(key, node)]
        case _:
            return []


@pytest.mark.parametrize("path", YAML_FILES, ids=YAML_IDS)
def test_no_pack_file_carries_a_credential(path):
    """§1, and the prohibition that cannot be walked back once broken.

    A key committed here would be in the history of a public repository forever, so
    the check is not "does review catch it" but "is the shape possible": every
    variable a pack names goes through `EnvVarName`, which an actual key cannot
    match, and every other value in the file is prose an operator wrote.

    The heuristic below is deliberately cruder than `failures.redact_secrets` — it
    is guarding five hand-written files, and a false positive here is a one-word
    rewording rather than a redacted report.
    """
    for key, value in _scalars(yaml.safe_load(path.read_text(encoding="utf-8"))):
        if key in _ENV_VAR_KEYS:
            assert _ENV_VAR_NAME.match(value), f"{key}: {value!r} is not a variable"
            continue
        assert _KEY_SHAPED.search(value) is None, \
            f"{path.name} has a key-shaped value under {key!r}"


def test_the_swiss_pack_names_a_variable_and_never_a_value():
    """The positive half: `sources.yaml` does declare a credential — by name.

    Asserted here because the test above would also pass on a pack that named no
    variable at all, and jooble's key is the one real credential in the phase.
    """
    bindings = yaml.safe_load((CH_DIR / "sources.yaml").read_text(
        encoding="utf-8"))["sources"]
    declared = {name for binding in bindings
                for name in binding.get("config_env_vars", ())}
    assert declared == {"JOOBLE_API_KEY"}
