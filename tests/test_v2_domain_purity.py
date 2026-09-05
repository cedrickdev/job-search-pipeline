# tests/test_v2_domain_purity.py
"""The domain's independence, asserted instead of merely documented.

Phase 1 requires that the V2 domain hold "pure models, enums, value objects, and
validation rules only", independent of SQLite, SQLAlchemy, FastAPI, Playwright,
Claude, Codex, OpenAI and every job board. A rule that lives only in prose decays
on the first hurried import, so these tests read the import graph instead.

The analysis is static (`ast`). An in-process `sys.modules` check would prove
nothing: by the time any test runs, `tests/conftest.py` has already imported
sqlite3 and FastAPI for the V1 fixtures. One test therefore checks the real
transitive closure in a subprocess, where nothing else has been imported yet.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_PACKAGE = "backend.app.domain"
DOMAIN_DIR = REPO_ROOT / "backend" / "app" / "domain"
COMPAT_DIR = REPO_ROOT / "backend" / "app" / "compat"

# The dependency order the domain's own `__init__` docstring declares. Pinned
# here so the layering is checked rather than described: every import edge has to
# point backwards in this tuple, which is what keeps the graph acyclic and keeps
# `eligibility` from growing into `matching`.
DEPENDENCY_ORDER = (
    "base", "identifiers", "user", "common", "opportunity", "company", "candidate",
    "search", "matching", "eligibility", "policy", "decision",
)

# Named explicitly rather than derived, so the list reads as the phase order's
# prohibition. The allowlist test below is the general rule; this one exists so a
# violation fails with the name of the thing that was reached for.
FORBIDDEN_ROOTS = frozenset({
    "sqlite3", "sqlalchemy", "alembic", "psycopg", "psycopg2", "asyncpg",
    "fastapi", "starlette", "uvicorn", "sse_starlette",
    "playwright", "selenium",
    "anthropic", "openai", "claude_code_sdk", "litellm", "tiktoken",
    "requests", "httpx", "aiohttp", "bs4", "redis",
    "yaml", "jinja2", "weasyprint", "openpyxl", "dotenv",
    "pipeline", "server", "dashboard", "tests",
})

# Only `pydantic` is allowed beyond the standard library: it is what "typed
# contracts" is implemented with. `annotated_types` and `typing_extensions` are
# pydantic's own dependencies and are legitimate transitively, but nothing in the
# domain imports them directly, so they are not listed.
ALLOWED_ROOTS = frozenset(sys.stdlib_module_names) | {"pydantic", "backend"}

DOMAIN_MODULES = tuple(sorted(path.stem for path in DOMAIN_DIR.glob("*.py")
                              if path.stem != "__init__"))


def _import_roots(path):
    """The root package of every import in one file."""
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # `level` means a relative import, which by definition stays inside
            # `backend` — it cannot reach infrastructure.
            roots.add("backend" if node.level else (node.module or "").split(".")[0])
    return roots


def _imported_domain_modules(path):
    """The sibling domain modules one file imports, by bare name."""
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                f"{DOMAIN_PACKAGE}."):
            imported.add(node.module[len(DOMAIN_PACKAGE) + 1:].split(".")[0])
    return imported


def _domain_path(module):
    return DOMAIN_DIR / f"{module}.py"


def test_the_domain_package_is_the_modules_its_docstring_lists():
    """A new module has to be placed in the declared order, not just dropped in."""
    assert DOMAIN_MODULES == tuple(sorted(DEPENDENCY_ORDER))


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_no_domain_module_reaches_for_infrastructure(module):
    """§Phase 1: no database, no web framework, no browser, no LLM client."""
    reached = _import_roots(_domain_path(module)) & FORBIDDEN_ROOTS
    assert reached == set(), f"{module} imports {sorted(reached)}"

@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_a_domain_module_imports_only_the_stdlib_and_pydantic(module):
    """The general form of the rule: an allowlist, so a *new* client is caught too.

    `FORBIDDEN_ROOTS` names today's temptations; this catches tomorrow's, which
    is the one that would otherwise arrive unnoticed.
    """
    unexpected = _import_roots(_domain_path(module)) - ALLOWED_ROOTS
    assert unexpected == set(), f"{module} imports {sorted(unexpected)}"


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_no_domain_module_imports_the_compatibility_layer(module):
    """The V1 shim depends on the domain; the domain must not learn about V1."""
    assert "compat" not in _import_roots(_domain_path(module))
    assert "compat" not in _imported_domain_modules(_domain_path(module))


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_every_import_edge_points_backwards_in_the_declared_order(module):
    """An acyclic layering, checked edge by edge rather than trusted."""
    position = DEPENDENCY_ORDER.index(module)
    for imported in _imported_domain_modules(_domain_path(module)):
        assert imported in DEPENDENCY_ORDER, f"{imported} is not placed in the order"
        assert DEPENDENCY_ORDER.index(imported) < position, \
            f"{module} imports {imported}, which comes after it"


def test_matching_and_eligibility_stay_out_of_each_others_way():
    """"Separate eligibility from compatibility" — as an import rule.

    If either could see the other, averaging a closed permit gate into `overall`
    would become one line of plausible code away.
    """
    assert "eligibility" not in _imported_domain_modules(_domain_path("matching"))
    assert "matching" not in _imported_domain_modules(_domain_path("eligibility"))


def test_the_decision_is_the_one_place_the_two_halves_meet():
    imported = _imported_domain_modules(_domain_path("decision"))
    assert {"matching", "eligibility"} <= imported


def test_a_policy_needs_scores_but_not_gates():
    """`allow_incomplete_eligibility` is a flag the caller reads, not a gate check.

    Importing `eligibility` here would invite the policy to re-derive a verdict
    the eligibility module already owns.
    """
    imported = _imported_domain_modules(_domain_path("policy"))
    assert "matching" in imported
    assert "eligibility" not in imported

# Names that would make a model depend on the moment it was constructed. The
# policy tests state the reason in one line: the caller owns the clock, and a
# domain object that read one would be untestable.
CLOCK_NAMES = frozenset({"now", "utcnow", "today", "monotonic", "perf_counter"})

# A domain object that printed, read a file or evaluated a string would be doing
# infrastructure work — and `print` is also how a secret reaches a terminal.
SIDE_CHANNEL_CALLS = frozenset({"print", "open", "input", "eval", "exec", "compile"})


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_no_domain_module_reads_a_clock(module):
    """Timestamps are passed in, never sampled: `NOW` in the tests is the proof."""
    for node in ast.walk(ast.parse(_domain_path(module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute):
            assert node.attr not in CLOCK_NAMES, f"{module} reads {node.attr}()"
        if isinstance(node, ast.Name):
            assert node.id not in CLOCK_NAMES, f"{module} reads {node.id}()"


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_no_domain_module_prints_reads_or_evaluates_anything(module):
    for node in ast.walk(ast.parse(_domain_path(module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in SIDE_CHANNEL_CALLS, \
                f"{module} calls {node.func.id}()"


COMPAT_MODULES = tuple(sorted(path.stem for path in COMPAT_DIR.glob("*.py")
                              if path.stem != "__init__"))

# The compatibility layer holds two kinds of module, and they do not get the same
# rule. A *mapping* module turns a `Mapping` into a domain object and must stay
# usable with no database at all. A *reader* module is the run that feeds it, so
# opening V1's SQLite file is precisely its job.
#
# Listed by name rather than derived, and the exhaustiveness test below is what
# makes a third module a decision someone took instead of a gap nobody noticed.
V1_MAPPING_MODULES = ("v1_jobs",)
V1_READER_MODULES = ("v1_import",)

# What a reader may add to the mapping rule: V1's own storage driver, and the two
# SQLAlchemy exception types it classifies a refused row by.
READER_EXTRA_ROOTS = frozenset({"sqlite3", "sqlalchemy"})

# And what it still may not touch. `sqlalchemy.orm` is the one worth naming: the
# importer takes a repository Protocol and a savepoint factory, so a module here
# that could open a session or build a query would be an infrastructure module
# filed in the wrong directory.
READER_FORBIDDEN_MODULES = ("sqlalchemy.orm", "sqlalchemy.ext.asyncio",
                            "sqlalchemy.future", "sqlalchemy.sql")


def _imported_modules(path):
    """Every import in one file, by full dotted name."""
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            modules.add(node.module or "")
    return modules


def test_every_compatibility_module_is_classified():
    """A new file in `compat/` picks a rule, or this fails.

    Without this, adding `v1_applications.py` would silently inherit whichever of
    the two tests below happened to be parametrized loosely.
    """
    assert COMPAT_MODULES == tuple(sorted(V1_MAPPING_MODULES + V1_READER_MODULES))


@pytest.mark.parametrize("module", V1_MAPPING_MODULES)
def test_the_mapping_layer_reads_v1_data_not_v1_code(module):
    """`opportunity_from_v1_job` takes a `Mapping`, so V1 needs no change to be read.

    Importing `pipeline` or `sqlite3` here would couple V2 to V1's storage and
    make the mapper unusable without a database — while the tests prove the
    mapping against a real V1 row anyway, by passing `dict(sqlite3.Row)` in.
    """
    roots = _import_roots(COMPAT_DIR / f"{module}.py")
    assert roots & FORBIDDEN_ROOTS == set()
    assert roots <= ALLOWED_ROOTS, f"{module} imports {sorted(roots - ALLOWED_ROOTS)}"


@pytest.mark.parametrize("module", V1_READER_MODULES)
def test_the_v1_reader_opens_sqlite_and_still_not_v1_code(module):
    """The importer reads V1's *file*; it must not import V1's *code*.

    `sqlite3` is how a V1 database is opened read-only, and `sqlalchemy.exc` is
    how a refused row is told apart from an unreachable database. Everything else
    on the list stays forbidden — above all `pipeline`, since needing V1's own
    modules to read V1's data is what would make this a fork of V1 rather than a
    migration away from it.
    """
    path = COMPAT_DIR / f"{module}.py"
    roots = _import_roots(path)
    reached = roots & (FORBIDDEN_ROOTS - READER_EXTRA_ROOTS)
    assert reached == set(), f"{module} imports {sorted(reached)}"
    assert roots <= ALLOWED_ROOTS | READER_EXTRA_ROOTS, \
        f"{module} imports {sorted(roots - ALLOWED_ROOTS - READER_EXTRA_ROOTS)}"
    imported = _imported_modules(path)
    for forbidden in READER_FORBIDDEN_MODULES:
        assert not any(name == forbidden or name.startswith(f"{forbidden}.")
                       for name in imported), f"{module} imports {forbidden}"


def test_nothing_forbidden_is_pulled_in_transitively():
    """The other tests read import lines; this one imports the package for real.

    A dependency could reach infrastructure on the domain's behalf, and that is
    what no static scan sees. It runs in a subprocess because in this one
    `conftest` imported sqlite3 and FastAPI long before any test started.
    """
    program = (
        "import importlib, sys\n"
        f"for name in {list(DOMAIN_MODULES)!r}:\n"
        f"    importlib.import_module('{DOMAIN_PACKAGE}.' + name)\n"
        "print(' '.join(sorted({name.split('.')[0] for name in sys.modules})))\n"
    )
    finished = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT,
                              capture_output=True, text=True, check=False)
    assert finished.returncode == 0, finished.stderr
    loaded = set(finished.stdout.split())
    assert "pydantic" in loaded, "the subprocess did not really import the domain"
    assert loaded & FORBIDDEN_ROOTS == set()
