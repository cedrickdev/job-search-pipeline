"""Production startup validation (§53-55): fail closed, never fall back to a dev credential.

Development is forgiving by design — an unset database URL becomes a loopback dev database, an
unset Redis URL becomes the local dev Redis, an unset public origin becomes `http://localhost`.
That is exactly what a developer wants and exactly what a production deployment must never get: a
silent fallback to a development credential is how a live service ends up writing to a throwaway
database or minting cookies a browser will not return over HTTPS.

So a deployment declares itself with `JOBSEARCH_ENV=production`, and this module refuses to let the
application start until the configuration a production feature actually needs is present and is not
a development default. It **fails closed** (§53): `validate_production_readiness` raises
`ProductionConfigError` listing *every* problem at once — an operator fixes one deployment rather
than rediscovering the next missing secret on the next boot — and the message names only variable
*names*, never a value, so nothing secret (not even a dev default) is ever echoed (§41).

The check is conditional on the feature being switched on (§53): billing secrets are required only
when `JOBSEARCH_BILLING_ENABLED` is set, and the credential-encryption master key only when
`JOBSEARCH_LLM_CREDENTIAL_ENCRYPTION_ENABLED` is set. The always-on essentials — a real database, a
real Redis, an https public origin, and a `Secure` cookie (§54) — are required unconditionally,
because every production deployment serves authenticated traffic over them.

Outside production mode this is a no-op, so development and the whole test suite are never gated:
`create_app` calls `validate_production_readiness()` on startup and it returns immediately unless
`JOBSEARCH_ENV=production`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from os import environ
from typing import Final

from backend.app.core.settings import (
    AUTH_COOKIE_SECURE_VARIABLE,
    DATABASE_URL_VARIABLES,
    DEFAULT_PUBLIC_BASE_URL,
    LLM_SECRET_KEY_VARIABLE,
    LOCAL_DEV_DATABASE_URL,
    SITE_PUBLIC_BASE_URL_VARIABLE,
    STRIPE_SECRET_KEY_VARIABLE,
    STRIPE_WEBHOOK_SECRET_VARIABLE,
)
from backend.app.tasks.settings import LOCAL_DEV_REDIS_URL

DEPLOYMENT_MODE_VARIABLE: Final[str] = "JOBSEARCH_ENV"
BILLING_ENABLED_VARIABLE: Final[str] = "JOBSEARCH_BILLING_ENABLED"
LLM_CREDENTIAL_ENCRYPTION_ENABLED_VARIABLE: Final[str] = (
    "JOBSEARCH_LLM_CREDENTIAL_ENCRYPTION_ENABLED")

# The Redis URL is resolved most-specific-first, the same order the queue and the rate limiter use;
# owned here as a public constant because production validation is the one place that must ask "is
# a real Redis configured?" without reaching into either leaf's private tuple.
REDIS_URL_VARIABLES: Final[tuple[str, str]] = ("JOBSEARCH_REDIS_URL", "REDIS_URL")

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSEY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


class DeploymentMode(str, Enum):
    """Which posture the process runs under: development (forgiving, the default) or production
    (fail-closed, §53). A str-enum so `JOBSEARCH_ENV=production` maps straight to a member."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class ProductionConfigError(RuntimeError):
    """Raised at startup when production mode is missing critical configuration (§53).

    Carries every problem found (not just the first) so one fix makes the deployment bootable. The
    string names only variable *names* — never a value — so a secret is never echoed (§41)."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        bullet = "\n  - "
        super().__init__(
            "refusing to start in production with invalid configuration:"
            + bullet + bullet.join(self.problems))


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return environ if env is None else env


def _first_set(source: Mapping[str, str], names: Sequence[str]) -> str:
    """The first of `names` with a non-blank value, or the empty string — the resolution the
    database, queue and rate limiter all share, applied here only to detect presence."""
    return next((source[name] for name in names if source.get(name, "").strip()), "")


def _is_true(source: Mapping[str, str], name: str) -> bool:
    return source.get(name, "").strip().lower() in _TRUTHY


def _is_false(source: Mapping[str, str], name: str) -> bool:
    return source.get(name, "").strip().lower() in _FALSEY


def deployment_mode(env: Mapping[str, str] | None = None) -> DeploymentMode:
    """The declared mode, defaulting to development. An unrecognised value fails closed (§53)."""
    raw = _source(env).get(DEPLOYMENT_MODE_VARIABLE, "").strip().lower()
    if not raw:
        return DeploymentMode.DEVELOPMENT
    try:
        return DeploymentMode(raw)
    except ValueError:
        raise ProductionConfigError([
            f"{DEPLOYMENT_MODE_VARIABLE} must be one of "
            f"{', '.join(mode.value for mode in DeploymentMode)}; got {raw!r}"]) from None
def production_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """Every reason a production deployment must not start, collected (empty list = ready).

    Pure and side-effect-free, so a readiness surface or a test can ask "would production boot?"
    without starting anything. `validate_production_readiness` is the enforcing wrapper; the checks
    are ordered essentials-first so the message reads like a checklist.
    """
    source = _source(env)
    problems: list[str] = []

    database_url = _first_set(source, DATABASE_URL_VARIABLES).strip()
    if not database_url:
        problems.append(
            f"set {DATABASE_URL_VARIABLES[0]}: production must not fall back to the local "
            "development database")
    elif database_url == LOCAL_DEV_DATABASE_URL:
        problems.append(
            f"{DATABASE_URL_VARIABLES[0]} is the local development database URL; point it at the "
            "production database")

    redis_url = _first_set(source, REDIS_URL_VARIABLES).strip()
    if not redis_url:
        problems.append(
            f"set {REDIS_URL_VARIABLES[0]}: the task queue and the rate limiter need a real Redis")
    elif redis_url == LOCAL_DEV_REDIS_URL:
        problems.append(
            f"{REDIS_URL_VARIABLES[0]} is the local development Redis URL; point it at the "
            "production Redis")

    origin = source.get(SITE_PUBLIC_BASE_URL_VARIABLE, "").strip()
    if not origin:
        problems.append(
            f"set {SITE_PUBLIC_BASE_URL_VARIABLE}: the public application origin is required to "
            "build billing redirect URLs and is never inferred from a request")
    elif origin.rstrip("/") == DEFAULT_PUBLIC_BASE_URL:
        problems.append(
            f"{SITE_PUBLIC_BASE_URL_VARIABLE} is the local development origin; set the public "
            "production origin")
    elif not origin.startswith("https://"):
        problems.append(
            f"{SITE_PUBLIC_BASE_URL_VARIABLE} must be an https:// origin in production so the "
            "Secure session cookie is returned by the browser (§54)")

    if _is_false(source, AUTH_COOKIE_SECURE_VARIABLE):
        problems.append(
            f"{AUTH_COOKIE_SECURE_VARIABLE} must not be turned off in production; the session "
            "cookie is Secure by default and cookie security is never inferred from scheme (§54)")

    if _is_true(source, BILLING_ENABLED_VARIABLE):
        if not source.get(STRIPE_SECRET_KEY_VARIABLE, "").strip():
            problems.append(
                f"{BILLING_ENABLED_VARIABLE} is on but {STRIPE_SECRET_KEY_VARIABLE} is unset; the "
                "billing provider secret is required to open a checkout or portal")
        if not source.get(STRIPE_WEBHOOK_SECRET_VARIABLE, "").strip():
            problems.append(
                f"{BILLING_ENABLED_VARIABLE} is on but {STRIPE_WEBHOOK_SECRET_VARIABLE} is unset; "
                "the webhook signing secret is required to verify inbound billing events")

    if _is_true(source, LLM_CREDENTIAL_ENCRYPTION_ENABLED_VARIABLE):
        if not source.get(LLM_SECRET_KEY_VARIABLE, "").strip():
            problems.append(
                f"{LLM_CREDENTIAL_ENCRYPTION_ENABLED_VARIABLE} is on but {LLM_SECRET_KEY_VARIABLE} "
                "is unset; the master key is required to encrypt stored LLM credentials at rest")

    return problems


def validate_production_readiness(env: Mapping[str, str] | None = None) -> None:
    """Fail closed (§53): raise `ProductionConfigError` if production is missing critical config.

    A no-op unless `JOBSEARCH_ENV=production`, so development and the test suite are never gated.
    Call it once at process startup — `create_app` does, and so does the worker entrypoint.
    """
    source = _source(env)
    if deployment_mode(source) is not DeploymentMode.PRODUCTION:
        return
    problems = production_problems(source)
    if problems:
        raise ProductionConfigError(problems)
