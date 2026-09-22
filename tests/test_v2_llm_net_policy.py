# tests/test_v2_llm_net_policy.py
"""The SSRF gate on a user-supplied base URL (§16-18, §89).

A base URL a user types is a URL the server will then fetch, so it is validated
before any client is built. These tests pin what the gate refuses (a non-http scheme,
the cloud metadata endpoint, a plaintext remote host, a "local" provider pointed off
the box) and how it classifies what it allows — because the `LOCAL_EXECUTION`
capability, and therefore the `LOCAL_ONLY` privacy guarantee, is set from that
classification, never from a connection's label.
"""
import pytest

from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers.net_policy import (
    AddressClass,
    validate_base_url,
)


def _misconfigured(base_url: str, *, require_local: bool = False):
    with pytest.raises(LLMError) as caught:
        validate_base_url(base_url, require_local=require_local)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    return caught.value


# --- refusals --------------------------------------------------------------

def test_a_non_http_scheme_is_refused():
    _misconfigured("file:///etc/passwd")
    _misconfigured("ftp://example.com")


def test_the_cloud_metadata_endpoint_is_refused_for_everyone():
    _misconfigured("http://169.254.169.254/latest/meta-data/")


def test_a_link_local_ipv6_is_refused():
    _misconfigured("http://[fe80::1]/v1")


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


# --- classification --------------------------------------------------------

def test_a_loopback_literal_is_local():
    result = validate_base_url("http://127.0.0.1:11434/v1", require_local=True)
    assert result.address_class is AddressClass.LOOPBACK
    assert result.is_local is True


def test_localhost_is_local():
    result = validate_base_url("http://localhost:1234/v1", require_local=True)
    assert result.address_class is AddressClass.LOOPBACK


def test_a_private_address_is_local_but_not_loopback():
    result = validate_base_url("https://10.0.0.5/v1", require_local=False)
    assert result.address_class is AddressClass.PRIVATE
    assert result.is_local is True
    # ...and it is refused for a require_local provider, which demands loopback.
    _misconfigured("https://10.0.0.5/v1", require_local=True)


def test_a_public_https_host_is_remote():
    result = validate_base_url("https://api.example.com/v1", require_local=False)
    assert result.address_class is AddressClass.REMOTE
    assert result.is_local is False


def test_the_normalized_url_has_no_trailing_slash():
    result = validate_base_url("https://api.example.com/v1/", require_local=False)
    assert result.normalized == "https://api.example.com/v1"
