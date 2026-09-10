# tests/test_v2_company_boundaries.py
"""Acceptance criterion 9, and §§6, 16–19 and 25, read off the import graph.

"The orchestrator has no hard-coded provider list" is a statement about *shape*,
and no behavioural test can see it: a discovery pass would pass its tests just as
well with `if provider_key == "configured_ats"` in the middle of `_run`, and the
day somebody writes that line the tests that notice are the ones that read the
code. The same is true of "adapters discover, application services decide what
gets persisted" (§17) and of "do not use an LLM for canonical identity decisions"
(§25) — both are absences, and an absence is checked statically or not at all.

So these are static (`ast`, `Path.read_text`), and nothing here imports the modules
it inspects, which is what lets it assert what is *not* there.

The layering (§17, §18) it pins:

    contracts → failures → {ats, identity, registry} → {resolution, orchestrator}
              → providers → bootstrap

`bootstrap` is the only module allowed to know which providers exist; a provider
may reach the contracts, the failure vocabulary and the ATS detector and nothing
above them; the whole package reaches no persistence, no LLM client and no browser.

Two rules of the phase order live elsewhere on purpose. §21 — companies are shared
facts and carry no `user_id` — is asserted against `Base.metadata` in
`tests/test_v2_persistence_schema.py`, where the other ownership rules already are.
§19's *behaviour* — a pack supplies suffixes, the service applies them — is in
`tests/test_v2_company_identity.py`; what is here is the import that would let a
core module read a country instead of a contract.

The analysis is repeated rather than imported from
`tests/test_v2_discovery_boundaries.py`: two boundary suites that shared their
analysis would fail as a pair, and thirty lines of `ast` walking cost less than
that coupling. One consequence of how they work is worth stating, because it looks
like a loophole: the prose in these modules names providers constantly —
`contracts.py` opens by explaining what a company provider is *not*, and
`__init__.py` contrasts it with `OpportunitySource`. That is the justification of a
rule living beside it, and a docstring carries no behaviour, so the word tests read
identifiers and live string literals only. Comments never reach the AST at all.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPANIES_DIR = REPO_ROOT / "backend" / "app" / "companies"
PROVIDERS_DIR = COMPANIES_DIR / "providers"
SERVICES_DIR = REPO_ROOT / "backend" / "app" / "services"
DISCOVERY_DIR = REPO_ROOT / "backend" / "app" / "discovery"

COMPANIES_PACKAGE = "backend.app.companies"

# The dependency order the package declares, contracts first. Pinned here so the
# layering is checked edge by edge: every import inside `backend/app/companies` has
# to point backwards in this tuple, which is what keeps `registry` out of
# `resolution` and keeps both out of `bootstrap`.
FRAMEWORK_ORDER = ("contracts", "failures", "ats", "identity", "registry",
                   "resolution", "orchestrator", "bootstrap")

# Everything but the composition module. These seven files are what "core" means in
# criterion 9, and none of them may name a provider.
CORE_MODULES = tuple(module for module in FRAMEWORK_ORDER if module != "bootstrap")

PROVIDER_MODULES = ("base", "configured_ats", "manual_seed", "stored_opportunities")

# The three keys `bootstrap` registers, and the classes behind them. Spelled out
# rather than imported: importing the providers would make these tests pass by
# construction the day one is renamed, and the point is that these words do not
# belong in the core whatever the providers are currently called.
PROVIDER_KEYS = frozenset({"configured_ats", "manual_seed", "stored_opportunities"})
PROVIDER_CLASSES = frozenset({"ConfiguredAtsCompanyProvider",
                              "ManualSeedCompanyProvider",
                              "StoredOpportunityCompanyProvider"})

# What a provider may reach for. Not `registry` or `orchestrator` (§14's isolation:
# an adapter must not learn about the sweep it runs in), and not `resolution` or
# `identity` — a provider that could resolve identity would be an adapter deciding
# which employer is canonical, which is exactly the split §17 draws.
PROVIDER_ALLOWED_FRAMEWORK = frozenset({"contracts", "failures", "ats"})

# Named so a violation fails with the name of the thing that was reached for. The
# allowlist beside it is the general rule that catches tomorrow's client.
#
# `sqlalchemy` and friends are §17: adapters discover, application services persist.
# `anthropic`, `openai` and the CLI SDKs are §25: Phase 6 decides identity without
# an LLM, and it has to keep working without one. `playwright` is §9: ATS detection
# reads host patterns and configured organization ids, and does not drive a browser.
INFRASTRUCTURE_ROOTS = frozenset({
    "requests", "httpx", "aiohttp", "urllib3", "bs4", "lxml", "selenium",
    "playwright", "anthropic", "openai", "claude_code_sdk", "litellm",
    "sqlite3", "sqlalchemy", "alembic", "psycopg", "asyncpg", "redis",
    "fastapi", "starlette", "uvicorn", "jinja2", "weasyprint", "openpyxl",
    "pipeline", "server", "dashboard", "tests",
})

ALLOWED_ROOTS = frozenset(sys.stdlib_module_names) | {
    "pydantic", "backend", "country_packs"}

# Where persistence lives. A company module importing either would be a discovery
# adapter that writes, which is the shape §17 forbids by name.
PERSISTENCE_PACKAGES = ("backend.app.repositories",
                        "backend.app.infrastructure")

# The same rule at the level of a name, for the dependency that arrives without an
# import: a repository passed in, a session held, a transaction committed.
PERSISTENCE_WORDS = ("repository", "sqlalchemy", "session", "commit")

# `country_packs.contracts` is configuration; `country_packs.ch` is a country. §19
# puts the rules in the pack and the workflow in the service, and a core module that
# imported Switzerland would have put one country inside the workflow.
PACK_ALLOWED_MODULES = frozenset({"contracts"})

COMPANY_SERVICES = ("company_directory", "company_discovery")

# Case is preserved, unlike the equivalent in the discovery suite, and the reason is
# a real collision: `CompanyProviderType.STORED_OPPORTUNITIES` describes *what kind
# of thing* a provider reads, while `stored_opportunities` is one provider's key. A
# provenance key is lower case by contract (`ProvenanceKey`), so the two spellings
# are different words — and folding them together would force the enum member out of
# the contracts module for no reason.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _prose(tree: ast.Module) -> set[int]:
    """Every string constant that is documentation: a docstring or a bare string.

    Identified by position rather than content — a string that is a statement all by
    itself explains something to a reader and cannot change what runs.
    """
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _executable_words(path: Path) -> set[str]:
    """Every word a module's *code* contains: identifiers and live string literals.

    Comments are absent from an AST and docstrings are subtracted, so what is left is
    what the interpreter acts on — the set a rule about behaviour has to be checked
    against.
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
        words |= set(_WORD.findall(text))
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
            # `from backend.app.companies import failures` and `from x.y import z`
            # name the same kind of thing; a dependency must not hide in the tail.
            modules |= {f"{module}.{alias.name}" for alias in node.names}
    return modules


def _import_roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imported_modules(path)}


def _framework_imports(path: Path) -> set[str]:
    """The `backend.app.companies` submodules a file imports, by bare name.

    Both spellings count: `from backend.app.companies import failures` and
    `from backend.app.companies.registry import CompanyProviderRegistry`.
    """
    prefix = f"{COMPANIES_PACKAGE}."
    return {module[len(prefix):].split(".")[0]
            for module in _imported_modules(path) if module.startswith(prefix)}


def _python_files(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in directory.rglob("*.py")
                        if "__pycache__" not in path.parts))


def _framework_path(module: str) -> Path:
    return COMPANIES_DIR / f"{module}.py"


def _provider_path(module: str) -> Path:
    return PROVIDERS_DIR / f"{module}.py"


PACKAGE_FILES = _python_files(COMPANIES_DIR)
PACKAGE_IDS = tuple(str(path.relative_to(REPO_ROOT)) for path in PACKAGE_FILES)


# --- criterion 9: the orchestrator names no provider --------------------------

def test_the_package_is_the_modules_the_layering_lists():
    """A new module has to be placed in the order, not merely dropped in.

    Without this, `merge.py` could appear tomorrow and inherit none of the rules
    below, because every test here is parametrized over these tuples.
    """
    assert tuple(sorted(path.stem for path in COMPANIES_DIR.glob("*.py")
                        if path.stem != "__init__")) == tuple(sorted(FRAMEWORK_ORDER))
    assert tuple(sorted(path.stem for path in PROVIDERS_DIR.glob("*.py")
                        if path.stem != "__init__")) == tuple(sorted(PROVIDER_MODULES))


@pytest.mark.parametrize("module", CORE_MODULES)
def test_no_core_module_names_a_provider_in_its_code(module):
    """Criterion 9, and the test that would catch `if key == "configured_ats"`.

    The prose in these files names providers on nearly every page — that is where a
    contract explains what it is for. What may not happen is a provider key or a
    provider class reaching an identifier, a comparison or a live string, because
    that is the hard-coded list the phase exists not to have.
    """
    words = _executable_words(_framework_path(module))
    named = words & (PROVIDER_KEYS | PROVIDER_CLASSES)
    assert named == frozenset(), f"{module} names {sorted(named)} in code"


@pytest.mark.parametrize("module", CORE_MODULES)
def test_only_the_composition_module_knows_which_providers_exist(module):
    """§18: registering a provider is composition, not orchestration."""
    imports = _framework_imports(_framework_path(module))
    assert "providers" not in imports
    assert "bootstrap" not in imports


def test_the_orchestrator_reaches_no_provider_and_no_country():
    """The sharp form of criterion 9: a pass cannot reach a concrete provider.

    `run()` receives a registry and a request. If it could import `providers` it
    could also fall back to one, and "adding a provider means editing the algorithm"
    would be one import away from returning.
    """
    orchestrator = _framework_path("orchestrator")
    assert _framework_imports(orchestrator) <= {"contracts", "failures", "registry"}
    assert "country_packs" not in _import_roots(orchestrator)


@pytest.mark.parametrize("module", FRAMEWORK_ORDER)
def test_every_import_inside_the_package_points_backwards(module):
    """An acyclic layering, edge by edge, so a cycle fails with both names."""
    position = FRAMEWORK_ORDER.index(module)
    for imported in _framework_imports(_framework_path(module)) - {"providers"}:
        assert imported in FRAMEWORK_ORDER, f"{imported} is not placed in the order"
        assert FRAMEWORK_ORDER.index(imported) < position, \
            f"{module} imports {imported}, which comes after it"


def test_the_composition_module_is_where_the_two_halves_meet():
    """Stated positively, so the rules above are not vacuously satisfied.

    If nothing wired the providers to the registry, every "the core names no
    provider" test would pass and a discovery pass would find nothing at all.
    """
    imports = _imported_modules(_framework_path("bootstrap"))
    assert {"orchestrator", "registry"} <= _framework_imports(_framework_path(
        "bootstrap"))
    assert {f"{COMPANIES_PACKAGE}.providers.{module}"
            for module in PROVIDER_MODULES} <= imports


# --- §17: adapters discover, application services persist ---------------------

@pytest.mark.parametrize("path", PACKAGE_FILES, ids=PACKAGE_IDS)
def test_nothing_in_the_package_can_reach_persistence(path):
    """§17, as an import rule: a provider that could write would eventually write.

    The phase order's wording is "do not put persistence writes inside raw provider
    adapters", and the reason is that a provider which persists decides what is
    canonical — which is the application service's decision, taken with the identity
    evidence a single provider does not have.
    """
    reached = sorted(module for module in _imported_modules(path)
                     if module.startswith(PERSISTENCE_PACKAGES))
    assert reached == [], f"{path.name} imports {reached}"


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=PACKAGE_IDS)
def test_nothing_in_the_package_can_call_an_llm_or_drive_a_browser(path):
    """§25 and §9 together, because both are absences.

    Phase 6 decides identity deterministically and must keep working with no model
    configured; ATS detection reads host patterns and configured organization ids
    rather than a rendered page. An allowlist rather than a blocklist, because the
    import that matters is the one nobody has thought of yet.
    """
    roots = _import_roots(path)
    assert roots & INFRASTRUCTURE_ROOTS == frozenset(), \
        f"{path.name} imports {sorted(roots & INFRASTRUCTURE_ROOTS)}"
    assert roots <= ALLOWED_ROOTS, \
        f"{path.name} imports {sorted(roots - ALLOWED_ROOTS)}"


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=PACKAGE_IDS)
def test_no_module_in_the_package_holds_a_repository_or_a_session(path):
    """The duck-typed form of the same rule, which an import test cannot see.

    `def __init__(self, repository)` needs no import to be a persistence dependency,
    and a provider handed a session would be one whatever it was annotated as. The
    words are matched as substrings, so `CompanyRepository` in a signature counts —
    which is exactly what `backend/app/services/company_directory.py` does declare
    and what nothing in this package may.

    What a provider takes instead is a callable returning already-loaded rows: a
    lister, not a store.
    """
    offending = sorted(word for word in _executable_words(path)
                       if any(name in word.lower() for name in PERSISTENCE_WORDS))
    assert offending == [], f"{path.name} holds {offending}"


@pytest.mark.parametrize("module", PROVIDER_MODULES)
def test_a_provider_never_learns_about_the_sweep_or_the_resolution(module):
    """§14's isolation and §17's split, in one import rule.

    A provider that could reach the registry could record its own health or read a
    sibling's; one that could reach `resolution` or `identity` would be an adapter
    deciding which employer is canonical. Both are decisions the orchestrator and
    the application service take *about* providers, with what all of them returned.

    The second assertion is the sibling rule: `base` is shared machinery, and a
    provider importing another provider would be a composition decision taken in the
    wrong file.
    """
    path = _provider_path(module)
    imports = _framework_imports(path) - {"providers"}
    assert imports <= PROVIDER_ALLOWED_FRAMEWORK, \
        f"{module} imports {sorted(imports - PROVIDER_ALLOWED_FRAMEWORK)}"

    prefix = f"{COMPANIES_PACKAGE}.providers."
    siblings = {name[len(prefix):].split(".")[0]
                for name in _imported_modules(path) if name.startswith(prefix)}
    assert siblings <= {"base"}, f"{module} imports {sorted(siblings)}"


@pytest.mark.parametrize("name", COMPANY_SERVICES)
def test_a_company_service_receives_contracts_and_never_a_session(name):
    """§16, stated in both directions.

    The application service is where persistence is decided, so it names
    repositories — but the *contracts*, never the SQLAlchemy implementations and
    never a session. That is what lets the whole service layer be tested against the
    fakes in `tests/v2_fakes.py`, and what keeps `AsyncSession` a detail the API
    composition root owns.
    """
    path = SERVICES_DIR / f"{name}.py"
    modules = _imported_modules(path)

    assert "backend.app.repositories.contracts" in modules
    assert "sqlalchemy" not in _import_roots(path)
    assert [module for module in modules
            if module.startswith("backend.app.infrastructure")] == []
    assert "backend.app.repositories.sqlalchemy_repositories" not in modules


# --- §6, §8, §19: what the package is allowed to know about ------------------

@pytest.mark.parametrize("path", PACKAGE_FILES, ids=PACKAGE_IDS)
def test_a_company_provider_is_never_an_opportunity_source(path):
    """§6: two protocols, deliberately not one.

    A company provider answers "which employers exist?" and a source answers "what
    is open right now?". The package explains that contrast in prose repeatedly —
    what it may not do is *use* the opportunity protocol, because a provider typed
    as one would have to answer both questions and the overloaded abstraction §6
    refuses would be back.
    """
    assert "OpportunitySource" not in _executable_words(path), \
        f"{path.name} uses OpportunitySource"


def test_the_discovery_package_does_not_depend_on_the_company_package():
    """The other direction of §6, which nothing else would notice.

    Companies reuse the source vocabulary — `SourceHealth`, `SourceFailureCode` —
    and that dependency points one way. A cycle would mean neither package could be
    read, tested or replaced on its own.
    """
    offenders = sorted(path.name for path in _python_files(DISCOVERY_DIR)
                       if any(module.startswith(COMPANIES_PACKAGE)
                              for module in _imported_modules(path)))
    assert offenders == []


def test_the_v1_company_configuration_is_reused_through_the_phase_5_adapter():
    """§8, positively: `config/companies.yaml` is read, and read where V1 is wrapped.

    Phase 5 put the V1 seam in `discovery/adapters/`, and Phase 6 reuses it instead
    of importing `pipeline` a second time or copying the organization ids into a
    Python list — which is the duplication §8 names.
    """
    configured = _imported_modules(_provider_path("configured_ats"))
    assert "backend.app.discovery.adapters.v1_sources" in configured
    assert all("pipeline" not in _import_roots(path) for path in PACKAGE_FILES)


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=PACKAGE_IDS)
def test_a_country_pack_is_read_through_its_contracts_and_never_by_name(path):
    """§19: the pack supplies configuration, the service performs the workflow.

    A module importing `country_packs.ch` would have put Switzerland inside the
    algorithm, and the second country would then be a change to this package rather
    than a new directory under `country_packs/`.
    """
    reached = {module.split(".")[1] for module in _imported_modules(path)
               if module.startswith("country_packs.")}
    assert reached <= PACK_ALLOWED_MODULES, \
        f"{path.name} reaches country_packs.{sorted(reached - PACK_ALLOWED_MODULES)}"
