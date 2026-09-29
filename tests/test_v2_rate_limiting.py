# tests/test_v2_rate_limiting.py
"""Rate limiting (Phase 16 §49-52): the fixed-window algorithm, the §51 identity, and the 429
behaviour over HTTP — plus the proof it is *additive* (§50), never a replacement for the
DB-backed account lockout.

Two layers, tested apart. The leaf (`backend.app.ratelimit`) is a pure fixed-window counter, so
its allow/deny/rollover behaviour is asserted against the in-memory limiter and `decide` directly
with a hand-moved clock — no app, no HTTP. The wiring (`backend.app.api.dependencies` + the
routes) is asserted over the real app through `api_harness` with tiny windows: an abuse-prone
endpoint answers 429 with a `Retry-After` header and a secret-free body once its window is spent,
buckets are independent per identity and per category, an unconfigured `X-Forwarded-For` cannot
mint fresh buckets, the window reopens when the clock passes it, the provider webhook is never
limited, and — the §50 property — a generous login limit still lets the 423 lockout fire first.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from starlette.requests import Request

from backend.app.api.dependencies import client_ip
from backend.app.core.settings import (
    RATE_LIMIT_ENABLED_VARIABLE,
    RATE_LIMIT_TRUSTED_PROXY_COUNT_VARIABLE,
    AuthSettings,
    RateLimitSettings,
)
from backend.app.ratelimit import (
    InMemoryRateLimiter,
    RateLimitCategory,
    RateLimitRule,
    decide,
    window_start,
)
from tests.v2_api import EMAIL, NOW, WRONG_PASSWORD, api_harness, credentials


# --- the leaf: the fixed-window counter, in memory, with a hand-moved clock ----------------

@pytest.mark.asyncio
async def test_in_memory_limiter_allows_up_to_max_then_denies():
    """The event that reaches exactly `max_events` is allowed; the next one is refused."""
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(max_events=3, window_seconds=60)
    for _ in range(3):
        decision = await limiter.check(namespace="login", identity="ip", rule=rule, now=NOW)
        assert decision.allowed
        assert decision.retry_after_seconds == 0
    denied = await limiter.check(namespace="login", identity="ip", rule=rule, now=NOW)
    assert not denied.allowed
    assert 1 <= denied.retry_after_seconds <= 60


@pytest.mark.asyncio
async def test_in_memory_limiter_reopens_when_the_window_passes():
    """A count belongs to one window: past its end the next event starts a fresh count."""
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(max_events=1, window_seconds=60)
    assert (await limiter.check(namespace="c", identity="i", rule=rule, now=NOW)).allowed
    blocked = await limiter.check(namespace="c", identity="i", rule=rule, now=NOW)
    assert not blocked.allowed
    later = await limiter.check(
        namespace="c", identity="i", rule=rule, now=NOW + timedelta(seconds=60))
    assert later.allowed


@pytest.mark.asyncio
async def test_in_memory_limiter_keeps_namespaces_and_identities_apart():
    """Two categories, or two identities, never share a bucket."""
    limiter = InMemoryRateLimiter()
    rule = RateLimitRule(max_events=1, window_seconds=60)
    assert (await limiter.check(namespace="a", identity="x", rule=rule, now=NOW)).allowed
    assert (await limiter.check(namespace="b", identity="x", rule=rule, now=NOW)).allowed
    assert (await limiter.check(namespace="a", identity="y", rule=rule, now=NOW)).allowed
    assert not (await limiter.check(namespace="a", identity="x", rule=rule, now=NOW)).allowed


def test_decide_allows_exactly_max_and_denies_the_next():
    rule = RateLimitRule(max_events=2, window_seconds=100)
    assert decide(1, rule, NOW).allowed
    assert decide(2, rule, NOW).allowed
    third = decide(3, rule, NOW)
    assert not third.allowed
    assert third.retry_after_seconds >= 1


def test_window_start_snaps_the_clock_down_to_the_window():
    epoch = int(NOW.timestamp())
    snapped = window_start(NOW, 60)
    assert snapped == epoch - (epoch % 60)
    assert snapped % 60 == 0


# --- the §51 identity: which network signal an anonymous request is counted by -------------

def _request(*, client, headers=None):
    """A minimal Starlette request, so `client_ip` can be exercised without an app."""
    header_pairs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": header_pairs, "client": client})


def test_client_ip_uses_the_socket_peer_when_no_proxy_is_trusted():
    """Default: the forwarding header is not trusted, so the socket peer is the identity (§51)."""
    request = _request(client=("203.0.113.7", 5000),
                       headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
    assert client_ip(request, trusted_proxy_count=0) == "203.0.113.7"


def test_client_ip_reads_the_entry_the_trusted_proxies_appended():
    """With N trusted proxies, the real client is the Nth-from-last forwarded entry."""
    request = _request(client=("10.0.0.1", 5000),
                       headers={"X-Forwarded-For": "198.51.100.9, 10.0.0.2"})
    assert client_ip(request, trusted_proxy_count=1) == "10.0.0.2"
    assert client_ip(request, trusted_proxy_count=2) == "198.51.100.9"


def test_client_ip_falls_back_to_the_peer_when_forwarded_is_short_or_absent():
    peer = _request(client=("203.0.113.7", 5000))
    assert client_ip(peer, trusted_proxy_count=1) == "203.0.113.7"
    short = _request(client=("203.0.113.7", 5000), headers={"X-Forwarded-For": "1.1.1.1"})
    assert client_ip(short, trusted_proxy_count=2) == "203.0.113.7"


def test_client_ip_degrades_to_a_fixed_label_when_there_is_no_socket_peer():
    assert client_ip(_request(client=None), trusted_proxy_count=0) == "unknown"


# --- the settings: the switch, the proxy count, the per-category windows -------------------

def test_disabled_settings_turn_the_switch_off_but_keep_valid_rules():
    settings = RateLimitSettings.disabled()
    assert settings.enabled is False
    assert settings.rule_for(RateLimitCategory.LOGIN).max_events >= 1


def test_from_env_reads_the_switch_the_proxy_count_and_a_category_window():
    settings = RateLimitSettings.from_env({
        RATE_LIMIT_ENABLED_VARIABLE: "false",
        RATE_LIMIT_TRUSTED_PROXY_COUNT_VARIABLE: "2",
        "JOBSEARCH_RATE_LIMIT_LOGIN_MAX_EVENTS": "7",
        "JOBSEARCH_RATE_LIMIT_LOGIN_WINDOW_SECONDS": "120",
    })
    assert settings.enabled is False
    assert settings.trusted_proxy_count == 2
    login = settings.rule_for(RateLimitCategory.LOGIN)
    assert (login.max_events, login.window_seconds) == (7, 120)
    # a category left unset keeps its safe default
    assert settings.rule_for(RateLimitCategory.EXPORT).max_events == 5


def test_from_env_defaults_are_enabled_with_no_trusted_proxy():
    settings = RateLimitSettings.from_env({})
    assert settings.enabled is True
    assert settings.trusted_proxy_count == 0


def test_redis_url_is_kept_out_of_the_settings_repr():
    """§41: the Redis URL can carry a password, so it never appears in the repr."""
    settings = RateLimitSettings.from_env(
        {"JOBSEARCH_REDIS_URL": "redis://user:s3cret@host:6379/0"})
    assert "s3cret" not in repr(settings)
    assert settings.redis_url == "redis://user:s3cret@host:6379/0"


# --- the wiring, over the real app: 429 behaviour, buckets, recovery, §50 -------------------

def _limited(category, *, max_events, window_seconds=60, trusted_proxy_count=0):
    """Rate limiting enabled with one tiny window; every other category keeps its default."""
    return RateLimitSettings(
        enabled=True, trusted_proxy_count=trusted_proxy_count,
        rules={category: RateLimitRule(max_events=max_events, window_seconds=window_seconds)})


@pytest.mark.asyncio
async def test_login_is_rate_limited_by_ip_with_a_secret_free_retry_after(tmp_path):
    """Once the login window is spent, a 429 carries the retry hint in the header and the body,
    and nothing an attacker could enumerate with (§52)."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.LOGIN,
                                                          max_events=2)) as api:
        for _ in range(2):
            assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401
        blocked = await api.log_in(password=WRONG_PASSWORD)
        assert blocked.status_code == 429
        body = blocked.json()
        assert set(body) == {"error", "detail", "retry_after"}
        assert body["error"] == "rate_limited"
        assert body["retry_after"] >= 1
        assert int(blocked.headers["retry-after"]) == body["retry_after"]
        # the reply names no email, no account, no category — it is one fixed sentence (§52)
        assert EMAIL not in blocked.text


@pytest.mark.asyncio
async def test_register_is_rate_limited_by_ip(tmp_path):
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.REGISTER,
                                                          max_events=2)) as api:
        assert (await api.register(email="a@example.com")).status_code == 201
        assert (await api.register(email="b@example.com")).status_code == 201
        blocked = await api.register(email="c@example.com")
        assert blocked.status_code == 429
        assert "retry-after" in blocked.headers


@pytest.mark.asyncio
async def test_a_user_endpoint_is_rate_limited_by_account(tmp_path):
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.EXPORT,
                                                         max_events=2)) as api:
        await api.sign_in()
        for _ in range(2):
            assert (await api.write("POST", "/me/exports")).status_code == 201
        blocked = await api.write("POST", "/me/exports")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1


@pytest.mark.asyncio
async def test_the_user_bucket_is_per_account_not_shared(tmp_path):
    """One account's exhausted export bucket does not spill onto another's."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.EXPORT,
                                                         max_events=1)) as api:
        await api.sign_in(email="first@example.com")
        assert (await api.write("POST", "/me/exports")).status_code == 201
        assert (await api.write("POST", "/me/exports")).status_code == 429
        # a different account (fresh cookies) has its own untouched bucket
        await api.sign_in(email="second@example.com")
        assert (await api.write("POST", "/me/exports")).status_code == 201


@pytest.mark.asyncio
async def test_categories_do_not_share_a_bucket(tmp_path):
    """Exhausting login must not lock out register — each category is its own namespace."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.LOGIN,
                                                         max_events=1)) as api:
        assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401
        assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 429
        assert (await api.register()).status_code == 201


@pytest.mark.asyncio
async def test_the_window_reopens_after_the_clock_passes_it(tmp_path):
    """A spent window is not a permanent ban (§51): past its end the account may act again."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.EXPORT,
                                                         max_events=1, window_seconds=60)) as api:
        await api.sign_in()
        assert (await api.write("POST", "/me/exports")).status_code == 201
        assert (await api.write("POST", "/me/exports")).status_code == 429
        api.clock.advance(timedelta(seconds=61))
        assert (await api.write("POST", "/me/exports")).status_code == 201


@pytest.mark.asyncio
async def test_the_ip_bucket_is_per_client_when_a_proxy_is_trusted(tmp_path):
    """With one trusted proxy, two forwarded clients are counted apart (§51)."""
    limits = _limited(RateLimitCategory.LOGIN, max_events=1, trusted_proxy_count=1)
    async with api_harness(tmp_path, rate_limits=limits) as api:
        body = credentials(password=WRONG_PASSWORD)
        first = {"X-Forwarded-For": "198.51.100.1"}
        second = {"X-Forwarded-For": "198.51.100.2"}
        assert (await api.client.post(api.url("/auth/login"), json=body,
                                      headers=first)).status_code == 401
        assert (await api.client.post(api.url("/auth/login"), json=body,
                                      headers=first)).status_code == 429
        # a different forwarded client has its own bucket
        assert (await api.client.post(api.url("/auth/login"), json=body,
                                      headers=second)).status_code == 401


@pytest.mark.asyncio
async def test_forwarded_header_is_ignored_without_configured_proxy_trust(tmp_path):
    """§51: with no trusted proxy, a varying X-Forwarded-For cannot mint fresh buckets — the
    socket peer is the only identity, so the limit still bites."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.LOGIN,
                                                         max_events=1)) as api:
        body = credentials(password=WRONG_PASSWORD)
        assert (await api.client.post(api.url("/auth/login"), json=body,
                                      headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 401
        # a fresh forwarded value must NOT escape the limit
        assert (await api.client.post(api.url("/auth/login"), json=body,
                                      headers={"X-Forwarded-For": "2.2.2.2"})).status_code == 429


@pytest.mark.asyncio
async def test_the_provider_webhook_is_never_rate_limited(tmp_path):
    """The session-less webhook (§64) carries no user or IP limit, so it never 429s."""
    async with api_harness(tmp_path, rate_limits=_limited(RateLimitCategory.CHECKOUT,
                                                         max_events=1)) as api:
        for _ in range(5):
            response = await api.client.post(
                api.url("/billing/webhook"), content=b"{}",
                headers={"stripe-signature": "t=1,v1=deadbeef"})
            assert response.status_code != 429
@pytest.mark.asyncio
async def test_rate_limiting_does_not_pre_empt_the_account_lockout(tmp_path):
    """§50: rate limiting is *additive*. A generous login limit must still let the DB-backed
    lockout (423) fire on the wrong-password count — the limiter can only add refusals, never
    remove the Phase 4 protection that bounds password guessing per account."""
    settings = AuthSettings.for_local_http().model_copy(update={"max_failed_logins": 3})
    limits = _limited(RateLimitCategory.LOGIN, max_events=50)
    async with api_harness(tmp_path, settings=settings, rate_limits=limits) as api:
        await api.sign_in()
        for _ in range(settings.max_failed_logins):
            assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401
        locked = await api.log_in()
        assert locked.status_code == 423
        assert locked.json()["error"] == "account_locked"


@pytest.mark.asyncio
async def test_the_default_harness_never_rate_limits(tmp_path):
    """The suite's default is rate limiting off, so its many repeated calls never spuriously 429."""
    async with api_harness(tmp_path) as api:
        for _ in range(6):
            assert (await api.log_in(password=WRONG_PASSWORD)).status_code == 401




