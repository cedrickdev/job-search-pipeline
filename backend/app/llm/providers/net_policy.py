"""Which base URLs a provider may talk to, and which are local.

An OpenAI-compatible provider is configured with a `base_url` a user supplies, and a
user-supplied URL the server then fetches is a server-side request forgery waiting to
happen (docs/LLM_PROVIDER_ARCHITECTURE.md §16-18, docs/ENGINEERING_STANDARDS.md
§Security). So a URL is validated before any client is built, against two questions
this module answers and nothing above it re-decides:

1. **Is the scheme allowed at all?** Only `http`/`https`. A `file://`, `ftp://` or
   `unix://` base URL is rejected outright — those are the shapes that turn "call my
   model server" into "read a file" or "hit an internal socket".
2. **Is this address local or remote, by the address itself — never the label?**
   (§89) A loopback or private address is *local*; anything else is *remote*. A
   remote base URL must be `https` (a plaintext prompt must not cross a network), and
   the metadata/link-local ranges are refused for both — `169.254.169.254` is the
   cloud metadata endpoint, and reaching it is the canonical SSRF payload.

A local provider (`LOCAL_OPENAI_COMPATIBLE`) additionally requires that the address
*be* loopback: an "Ollama" connection whose URL points at a public host is a
misconfiguration, not a local provider, and it is refused rather than quietly sending
the candidate's data off the machine.

Name resolution is deliberately *not* done here. A hostname like `internal.corp`
cannot be classified without a DNS lookup, and a lookup at validation time is both a
side effect and a TOCTOU gap. Instead a non-literal host is allowed only for remote
`https` URLs (where TLS and the remote policy already apply) and refused for a
local-only provider, which must name a loopback literal. This keeps the check pure
and total.
"""
import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from backend.app.llm.failures import LLMError, LLMFailureCode

_ALLOWED_SCHEMES = ("http", "https")

# The metadata service and the link-local range it lives in. Refused for every
# provider, local or remote: no legitimate model server is reached through
# 169.254.0.0/16, and that address is the SSRF target that matters most.
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")


class AddressClass(StrEnum):
    """Where a validated base URL points — decided by the address, not the label."""

    LOOPBACK = "LOOPBACK"
    PRIVATE = "PRIVATE"
    REMOTE = "REMOTE"

    @property
    def is_local(self) -> bool:
        """Loopback and private addresses are local; a public host is not.

        A private (RFC 1918) address is treated as local for the capability flag —
        it is on the operator's own network, not the public internet — while still
        being refused for a `LOCAL_OPENAI_COMPATIBLE` provider, which demands
        loopback specifically. The distinction the privacy class cares about is
        "does this leave the box/network?", and both loopback and private answer no.
        """
        return self in (AddressClass.LOOPBACK, AddressClass.PRIVATE)


@dataclass(frozen=True)
class ValidatedBaseUrl:
    """A base URL that passed the policy, with what the policy concluded about it.

    `normalized` is the URL with a trailing slash trimmed, so an adapter appends
    `/chat/completions` without doubling a slash. `address_class` drives the
    `LOCAL_EXECUTION` capability: an adapter sets it from *this*, not from a
    connection's name (§89).
    """

    normalized: str
    scheme: str
    host: str
    address_class: AddressClass

    @property
    def is_local(self) -> bool:
        return self.address_class.is_local


def _classify_host(host: str) -> AddressClass | None:
    """Classify a host literal, or `None` when it is a name needing resolution."""
    if host in ("localhost", "localhost.localdomain"):
        return AddressClass.LOOPBACK
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname, not a literal
    if address in _LINK_LOCAL_V4 or address.is_link_local:
        # Refused for everyone: the metadata endpoint and its range.
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL points at a link-local or metadata address")
    if address.is_loopback:
        return AddressClass.LOOPBACK
    # `is_private` already covers the IPv6 unique-local range (fc00::/7) and the IPv4
    # RFC1918 ranges, so it is the whole "private" test — there is no separate
    # `is_unique_local` on the stdlib address types.
    if address.is_private:
        return AddressClass.PRIVATE
    return AddressClass.REMOTE


def validate_base_url(base_url: str, *, require_local: bool) -> ValidatedBaseUrl:
    """Vet a user-supplied base URL, or raise `PROVIDER_MISCONFIGURED`.

    `require_local` is set by a `LOCAL_OPENAI_COMPATIBLE` provider: the address must
    then be a loopback literal, so a "local" connection cannot be pointed at a public
    host. A remote provider (`require_local=False`) accepts a public host but demands
    `https`, and accepts a private literal or a hostname on `https` — the network and
    TLS then bound the exposure the way the metadata refusal above bounds the worst
    case.
    """
    trimmed = base_url.strip()
    if not trimmed:
        raise LLMError(LLMFailureCode.PROVIDER_MISCONFIGURED,
                       detail="a base URL is required")
    parts = urlsplit(trimmed)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL scheme must be http or https")
    host = parts.hostname
    if not host:
        raise LLMError(LLMFailureCode.PROVIDER_MISCONFIGURED,
                       detail="the base URL has no host")

    address_class = _classify_host(host)  # raises on link-local/metadata

    if require_local:
        if address_class is not AddressClass.LOOPBACK:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="a local provider must point at a loopback address")
        # A loopback server over plain HTTP is fine — the data never leaves the box.
    else:
        if address_class is None:
            # A hostname for a remote provider: allowed only over https, where TLS
            # and the remote policy apply. The concrete address is resolved by the
            # client at call time, not here.
            address_class = AddressClass.REMOTE
        if address_class is AddressClass.REMOTE and parts.scheme != "https":
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="a remote provider must use https so the prompt is encrypted "
                       "in transit")

    return ValidatedBaseUrl(
        normalized=trimmed.rstrip("/"),
        scheme=parts.scheme,
        host=host,
        address_class=address_class)
