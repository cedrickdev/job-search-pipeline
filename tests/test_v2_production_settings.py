# tests/test_v2_production_settings.py
"""Production startup validation (Phase 16 §53-55): the fail-closed gate that refuses to boot a
production deployment on a development credential.

Two layers, tested apart. The pure predicate (`deployment_mode`, `production_problems`,
`validate_production_readiness`) is asserted against injected environment mappings — no process
env, no app — so every branch of "would production start?" is exercised in isolation. The wiring
(`server.app.create_app`) is asserted once end-to-end: with `JOBSEARCH_ENV=production` and nothing
else set, the factory raises before it builds anything; unset, it builds as always.

The §41 property is asserted explicitly: the error names only variable *names*, never a value, so a
misconfiguration message can never echo a secret an operator pasted into the wrong slot.
"""
from __future__ import annotations

import pytest

from backend.app.core.production import (
    BILLING_ENABLED_VARIABLE,
    LLM_CREDENTIAL_ENCRYPTION_ENABLED_VARIABLE,
    DeploymentMode,
    ProductionConfigError,
    deployment_mode,
    production_problems,
    validate_production_readiness,
)
from backend.app.core.settings import (
    LOCAL_DEV_DATABASE_URL,
    STRIPE_SECRET_KEY_VARIABLE,
    STRIPE_WEBHOOK_SECRET_VARIABLE,
)
from backend.app.tasks.settings import LOCAL_DEV_REDIS_URL

# A complete, valid production environment — every essential set to a non-dev value. Individual
# tests remove or corrupt one key at a time to prove exactly one problem appears.
_VALID_PRODUCTION = {
    "JOBSEARCH_ENV": "production",
    "JOBSEARCH_DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/prod",
    "JOBSEARCH_REDIS_URL": "redis://redis.internal:6379/0",
    "JOBSEARCH_PUBLIC_BASE_URL": "https://app.example.com",
}


def _production(**overrides: str) -> dict[str, str]:
    """The valid production env with `overrides` applied; a value of "" deletes the key."""
    env = dict(_VALID_PRODUCTION)
    for key, value in overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    return env


# --- deployment_mode: what posture the process declares (§53) --------------------------------


def test_deployment_mode_defaults_to_development_when_unset() -> None:
    """No `JOBSEARCH_ENV` means development — the forgiving default a developer expects."""
    assert deployment_mode({}) is DeploymentMode.DEVELOPMENT


def test_deployment_mode_reads_explicit_production() -> None:
    assert deployment_mode({"JOBSEARCH_ENV": "production"}) is DeploymentMode.PRODUCTION


def test_deployment_mode_is_case_and_whitespace_insensitive() -> None:
    assert deployment_mode({"JOBSEARCH_ENV": "  Production  "}) is DeploymentMode.PRODUCTION


def test_deployment_mode_rejects_an_unrecognised_value() -> None:
    """An unknown mode fails closed rather than silently falling back to development (§53)."""
    with pytest.raises(ProductionConfigError) as caught:
        deployment_mode({"JOBSEARCH_ENV": "staging"})
    (problem,) = caught.value.problems
    assert "JOBSEARCH_ENV" in problem
    assert "staging" in problem  # the offending mode is a name, not a secret — safe to echo


# --- validate_production_readiness: the enforcing wrapper -------------------------------------


def test_validate_is_a_no_op_in_development_even_with_everything_unset() -> None:
    """Development is never gated: an empty env is exactly the local-dev fallback path (§53)."""
    validate_production_readiness({})  # must not raise


def test_validate_is_a_no_op_in_development_with_dev_defaults() -> None:
    """The dev database/Redis/origin are legal in development — only production rejects them."""
    validate_production_readiness({
        "JOBSEARCH_DATABASE_URL": LOCAL_DEV_DATABASE_URL,
        "JOBSEARCH_REDIS_URL": LOCAL_DEV_REDIS_URL,
    })


def test_validate_passes_for_a_complete_production_environment() -> None:
    validate_production_readiness(_production())  # must not raise


def test_validate_raises_and_carries_every_problem() -> None:
    """A bare `JOBSEARCH_ENV=production` is missing every essential; the error lists them all so
    an operator fixes one deployment rather than one boot at a time (§53)."""
    with pytest.raises(ProductionConfigError) as caught:
        validate_production_readiness({"JOBSEARCH_ENV": "production"})
    problems = caught.value.problems
    assert len(problems) >= 3  # database, Redis and public origin at minimum
    # The rendered message is a bulleted checklist of the same problems.
    rendered = str(caught.value)
    for problem in problems:
        assert problem in rendered


# --- production_problems: one essential at a time --------------------------------------------


def test_production_valid_environment_has_no_problems() -> None:
    assert production_problems(_production()) == []


def test_production_requires_a_database_url() -> None:
    problems = production_problems(_production(JOBSEARCH_DATABASE_URL=""))
    assert any("JOBSEARCH_DATABASE_URL" in problem for problem in problems)


def test_production_rejects_the_local_dev_database_url() -> None:
    problems = production_problems(_production(JOBSEARCH_DATABASE_URL=LOCAL_DEV_DATABASE_URL))
    assert any("JOBSEARCH_DATABASE_URL" in problem for problem in problems)


def test_production_requires_a_redis_url() -> None:
    problems = production_problems(_production(JOBSEARCH_REDIS_URL=""))
    assert any("JOBSEARCH_REDIS_URL" in problem for problem in problems)


def test_production_rejects_the_local_dev_redis_url() -> None:
    problems = production_problems(_production(JOBSEARCH_REDIS_URL=LOCAL_DEV_REDIS_URL))
    assert any("JOBSEARCH_REDIS_URL" in problem for problem in problems)


def test_production_requires_a_public_origin() -> None:
    problems = production_problems(_production(JOBSEARCH_PUBLIC_BASE_URL=""))
    assert any("JOBSEARCH_PUBLIC_BASE_URL" in problem for problem in problems)


def test_production_rejects_a_non_https_public_origin() -> None:
    """A production origin must be https:// so the Secure session cookie is returned (§54)."""
    problems = production_problems(_production(JOBSEARCH_PUBLIC_BASE_URL="http://app.example.com"))
    assert any("JOBSEARCH_PUBLIC_BASE_URL" in problem for problem in problems)


def test_production_rejects_the_local_dev_public_origin() -> None:
    problems = production_problems(
        _production(JOBSEARCH_PUBLIC_BASE_URL="http://localhost:3000"))
    assert any("JOBSEARCH_PUBLIC_BASE_URL" in problem for problem in problems)


def test_production_rejects_turning_the_secure_cookie_off() -> None:
    """Cookie security is never inferred from scheme; turning it off in production is a problem
    (§54)."""
    problems = production_problems(_production(JOBSEARCH_AUTH_COOKIE_SECURE="false"))
    assert any("JOBSEARCH_AUTH_COOKIE_SECURE" in problem for problem in problems)


def test_production_allows_the_secure_cookie_left_at_its_default() -> None:
    """Unset means the Secure default — no problem; only an explicit false is rejected (§54)."""
    assert production_problems(_production()) == []


# --- conditional feature gating: only required when the feature is switched on (§53) ---------


def test_billing_disabled_needs_no_stripe_secrets() -> None:
    """The billing secrets are required only when billing is enabled — an unset provider is a
    legitimate production posture (billing simply off)."""
    assert production_problems(_production()) == []


def test_billing_enabled_requires_both_stripe_secrets() -> None:
    problems = production_problems(_production(JOBSEARCH_BILLING_ENABLED="true"))
    assert any(STRIPE_SECRET_KEY_VARIABLE in problem for problem in problems)
    assert any(STRIPE_WEBHOOK_SECRET_VARIABLE in problem for problem in problems)


def test_billing_enabled_with_only_the_provider_secret_still_needs_the_webhook_secret() -> None:
    problems = production_problems(_production(
        JOBSEARCH_BILLING_ENABLED="true",
        JOBSEARCH_STRIPE_SECRET_KEY="sk_live_placeholder"))  # noqa: S106 — a test placeholder
    assert not any(STRIPE_SECRET_KEY_VARIABLE in problem for problem in problems)
    assert any(STRIPE_WEBHOOK_SECRET_VARIABLE in problem for problem in problems)


def test_billing_enabled_and_fully_configured_has_no_billing_problem() -> None:
    problems = production_problems(_production(
        JOBSEARCH_BILLING_ENABLED="true",
        JOBSEARCH_STRIPE_SECRET_KEY="sk_live_placeholder",  # noqa: S106 — a test placeholder
        JOBSEARCH_STRIPE_WEBHOOK_SECRET="whsec_placeholder"))  # noqa: S106 — a test placeholder
    assert problems == []


def test_llm_credential_encryption_disabled_needs_no_master_key() -> None:
    assert production_problems(_production()) == []


def test_llm_credential_encryption_enabled_requires_the_master_key() -> None:
    problems = production_problems(
        _production(JOBSEARCH_LLM_CREDENTIAL_ENCRYPTION_ENABLED="true"))
    assert any(LLM_CREDENTIAL_ENCRYPTION_ENABLED_VARIABLE in problem for problem in problems)


def test_llm_credential_encryption_enabled_and_keyed_has_no_problem() -> None:
    problems = production_problems(_production(
        JOBSEARCH_LLM_CREDENTIAL_ENCRYPTION_ENABLED="true",
        JOBSEARCH_LLM_SECRET_KEY="a-32-byte-master-key-placeholder"))  # noqa: S106 — a placeholder
    assert problems == []


# --- §41: the message names variables, never values -----------------------------------------


def test_error_message_never_echoes_a_secret_value() -> None:
    """A missing webhook secret is reported by naming the *variable*, never by echoing the
    provider secret an operator did paste in (§41). Prove it: the pasted secret must not appear
    anywhere in the rendered error."""
    secret = "sk_live_super_secret_do_not_leak_1234567890"  # noqa: S105 — the value we prove never leaks
    with pytest.raises(ProductionConfigError) as caught:
        validate_production_readiness(_production(
            JOBSEARCH_BILLING_ENABLED="true", JOBSEARCH_STRIPE_SECRET_KEY=secret))
    rendered = str(caught.value)
    assert secret not in rendered
    assert STRIPE_WEBHOOK_SECRET_VARIABLE in rendered  # the *name* is what an operator needs


def test_error_message_never_echoes_a_misplaced_database_url() -> None:
    """Even the value that *is* wrong (a dev database URL) is never echoed — only its variable
    name — so nothing an operator pasted is ever reflected back (§41)."""
    with pytest.raises(ProductionConfigError) as caught:
        validate_production_readiness(_production(JOBSEARCH_DATABASE_URL=LOCAL_DEV_DATABASE_URL))
    assert LOCAL_DEV_DATABASE_URL not in str(caught.value)


# --- create_app wiring: production fails closed before it builds anything (§53) --------------


def test_create_app_fails_closed_in_production_without_config(monkeypatch: pytest.MonkeyPatch,
                                                              ) -> None:
    """The factory calls `validate_production_readiness` as its first act, so a production
    deployment missing critical config raises before a single resource is built."""
    from server.app import create_app

    monkeypatch.setenv("JOBSEARCH_ENV", "production")
    monkeypatch.delenv("JOBSEARCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("JOBSEARCH_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("JOBSEARCH_PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ProductionConfigError):
        create_app()


def test_create_app_builds_in_development(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Unset (development) the gate is a no-op and the factory builds as always — the whole
    existing suite depends on this."""
    from server.app import create_app

    monkeypatch.delenv("JOBSEARCH_ENV", raising=False)
    app = create_app(db_path=tmp_path / "dev.sqlite3")
    assert app.title == "Job Search Command Center"


