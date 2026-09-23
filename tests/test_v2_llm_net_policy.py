# tests/test_v2_llm_net_policy.py
"""The SSRF gate on a user-supplied base URL (§2-11, §16-18, §89).

A base URL a user types is a URL the server will then fetch, so it is validated in two
places: statically at construction (`validate_base_url`) and, for a hostname, against
its resolved addresses immediately before each request
(`resolve_and_validate_remote_host`). These tests pin both — what the static check
refuses and classifies, and that resolution refuses any answer that is not public —
because the `LOCAL_EXECUTION` capability, and therefore the `LOCAL_ONLY` privacy
guarantee, is set from that classification, never from a connection's label.

The one invariant everything below rests on: **local means the same machine, not the
same network.** Only a loopback address is local; a private-LAN (RFC1918 / IPv6 ULA)
address is neither local nor public, so it is refused for both provider kinds.
"""
import ipaddress

import pytest

from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers.net_policy import (
    AddressClass,
    resolve_and_validate_remote_host,
    validate_base_url,
    validate_resolved_addresses,
)
from tests.v2_llm import FakeHostResolver

pytestmark_async = pytest.mark.asyncio


def _misconfigured(base_url: str, *, require_local: bool = False):
    with pytest.raises(LLMError) as caught:
        validate_base_url(base_url, require_local=require_local)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    return caught.value


def _addresses(*literals: str):
    return tuple(ipaddress.ip_address(literal) for literal in literals)


# --- static refusals -------------------------------------------------------

def test_a_non_http_scheme_is_refused():
    _misconfigured("file:///etc/passwd")
    _misconfigured("ftp://example.com")


def test_the_cloud_metadata_endpoint_is_refused_for_everyone():
    _misconfigured("http://169.254.169.254/latest/meta-data/")


def test_a_link_local_ipv6_is_refused():
    _misconfigured("http://[fe80::1]/v1")


def test_an_unspecified_address_is_refused():
    _misconfigured("http://0.0.0.0/v1")


def test_a_remote_host_over_plain_http_is_refused():
    # A prompt to a public host must be encrypted in transit.
    _misconfigured("http://api.example.com/v1")


def test_an_empty_base_url_is_refused():
    _misconfigured("   ")


def test_a_base_url_without_a_host_is_refused():
    _misconfigured("https:///v1")


def test_a_local_provider_pointed_at_a_public_host_is_refused():
    _misconfigured("https://api.example.com/v1", require_local=True)


def test_a_local_provider_pointed_at_a_hostname_is_refused():
    # A local provider must name a loopback literal, not a name needing resolution.
    _misconfigured("http://my-server.corp/v1", require_local=True)


def test_a_local_provider_pointed_at_a_private_lan_address_is_refused():
    # A private-LAN address is off this machine, so it is not a local provider (§3).
    _misconfigured("http://10.0.0.5:11434/v1", require_local=True)
    _misconfigured("http://192.168.1.10:11434/v1", require_local=True)


def test_a_remote_provider_pointed_at_a_private_literal_is_refused():
    # A remote provider must be public: a private-LAN literal is refused statically (§4).
    _misconfigured("https://10.0.0.5/v1", require_local=False)


def test_a_remote_provider_pointed_at_a_loopback_literal_is_refused():
    # Loopback belongs to a local provider; a remote one pointed at it is refused.
    _misconfigured("https://127.0.0.1/v1", require_local=False)


# --- static classification -------------------------------------------------

def test_a_loopback_literal_is_local():
    result = validate_base_url("http://127.0.0.1:11434/v1", require_local=True)
    assert result.address_class is AddressClass.LOOPBACK
    assert result.is_local is True
    assert result.needs_runtime_resolution is False


def test_localhost_is_local():
    result = validate_base_url("http://localhost:1234/v1", require_local=True)
    assert result.address_class is AddressClass.LOOPBACK
    assert result.needs_runtime_resolution is False


def test_a_public_https_host_is_remote_and_needs_resolution():
    result = validate_base_url("https://api.example.com/v1", require_local=False)
    assert result.address_class is AddressClass.REMOTE
    assert result.is_local is False
    # A hostname's concrete addresses are unknown until the request-time lookup.
    assert result.needs_runtime_resolution is True


def test_a_public_ip_literal_is_remote_and_needs_no_resolution():
    result = validate_base_url("https://93.184.216.34/v1", require_local=False)
    assert result.address_class is AddressClass.REMOTE
    assert result.needs_runtime_resolution is False


def test_the_normalized_url_has_no_trailing_slash():
    result = validate_base_url("https://api.example.com/v1/", require_local=False)
    assert result.normalized == "https://api.example.com/v1"


def test_a_private_address_is_not_local():
    # The corrective's core semantic change: a private-LAN address is not local.
    assert AddressClass.PRIVATE_NETWORK.is_local is False
    assert AddressClass.LOOPBACK.is_local is True
    assert AddressClass.REMOTE.is_local is False


# --- resolved-address validation (§4, §6) ----------------------------------

def test_validate_resolved_addresses_accepts_a_public_answer():
    validate_resolved_addresses(_addresses("93.184.216.34"))  # does not raise


def test_validate_resolved_addresses_refuses_the_metadata_address():
    with pytest.raises(LLMError) as caught:
        validate_resolved_addresses(_addresses("169.254.169.254"))
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


def test_validate_resolved_addresses_refuses_a_private_answer():
    with pytest.raises(LLMError):
        validate_resolved_addresses(_addresses("10.0.0.5"))
    with pytest.raises(LLMError):
        validate_resolved_addresses(_addresses("192.168.0.10"))


def test_validate_resolved_addresses_refuses_a_loopback_answer():
    with pytest.raises(LLMError):
        validate_resolved_addresses(_addresses("127.0.0.1"))


def test_validate_resolved_addresses_refuses_an_ipv6_ula_answer():
    with pytest.raises(LLMError):
        validate_resolved_addresses(_addresses("fc00::1"))


def test_validate_resolved_addresses_refuses_a_mixed_answer():
    with pytest.raises(LLMError):
        validate_resolved_addresses(_addresses("93.184.216.34", "10.0.0.5"))


def test_validate_resolved_addresses_refuses_an_empty_answer():
    with pytest.raises(LLMError):
        validate_resolved_addresses(())


# --- resolve-and-validate through a fake resolver (§5, §7) -----------------

@pytest.mark.asyncio
async def test_resolve_and_validate_accepts_a_public_name():
    resolver = FakeHostResolver({"api.example.com": ("93.184.216.34",)})
    await resolve_and_validate_remote_host("api.example.com", resolver)  # no raise


@pytest.mark.asyncio
async def test_resolve_and_validate_refuses_a_name_resolving_to_private():
    resolver = FakeHostResolver({"internal.corp": ("10.0.0.5",)})
    with pytest.raises(LLMError) as caught:
        await resolve_and_validate_remote_host("internal.corp", resolver)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


@pytest.mark.asyncio
async def test_resolve_and_validate_normalizes_a_resolver_failure_without_leaking():
    # An unknown name raises in the resolver; the gate turns it into a typed error whose
    # detail carries none of the resolver's own text (§7, §25).
    resolver = FakeHostResolver({})
    with pytest.raises(LLMError) as caught:
        await resolve_and_validate_remote_host("nowhere.invalid", resolver)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    assert "fake resolver" not in caught.value.detail
    assert "nowhere.invalid" not in caught.value.detail
