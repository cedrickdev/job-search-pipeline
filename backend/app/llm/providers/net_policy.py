"""Which base URLs a provider may talk to, whether they are local, and — for a
hostname — what they resolve to at the moment of the request.

An OpenAI-compatible provider is configured with a `base_url` a user supplies, and a
user-supplied URL the server then fetches is a server-side request forgery waiting to
happen (docs/LLM_PROVIDER_ARCHITECTURE.md §16-18, docs/ENGINEERING_STANDARDS.md
§Security). Two checks stand between the URL and the socket:

1. **A static check at construction (`validate_base_url`).** Only `http`/`https`; the
   link-local, metadata, unspecified, multicast and reserved ranges refused for
   everyone; a remote provider forced onto `https` so a plaintext prompt never crosses
   a network; a `LOCAL_OPENAI_COMPATIBLE` provider forced onto a *loopback literal*.
   A literal address is classified here and never re-decided. A *hostname* cannot be
   classified without DNS, and a lookup at construction is both a side effect and a
   TOCTOU window (it can be re-pointed before the request), so it is deferred.
2. **A resolution check immediately before each request (`resolve_and_validate_remote_host`).**
   A remote hostname is resolved through an injected `HostResolver`, and the request is
   refused unless *every* answer is a public address (§4, §6). A loopback, private-LAN
   (RFC1918 / IPv6 ULA), link-local or metadata answer — even one among several — is a
   refusal, so a name that resolves to `169.254.169.254` or `10.0.0.5` cannot be used
   to reach inside the deployment.

**Local means the same machine, not the same network.** Only a loopback address is
`is_local`; an RFC1918 or IPv6 ULA address is a *private network* address, which is
neither loopback nor public — refused for a local provider (it is off the box) and
refused for a remote one (it is not public). That is what makes the `LOCAL_ONLY`
privacy class an honest "this prompt never leaves the machine" rather than "this
prompt stays on the LAN".

**A residual TOCTOU remains (§9).** Resolution happens immediately before the request,
but the socket is not pinned to the validated address, so a name that answers a public
IP for the policy check and a private IP for the connection (DNS rebinding) is not
fully closed here. Redirects are disabled at the client so a `302` cannot re-target,
and the window is the gap between the lookup and the connect. Pinning the socket to the
validated IP while preserving SNI would close it and is left as a follow-up; this
module does not claim complete rebinding protection.

The check never trusts a hostname *label* — the resolved address is authoritative
(§11). The only exception is the two explicit loopback aliases `localhost` and
`localhost.localdomain`, accepted as loopback on the local path without a lookup.
"""
import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from backend.app.llm.failures import LLMError, LLMFailureCode

_ALLOWED_SCHEMES = ("http", "https")

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# The metadata service and the link-local range it lives in. Refused for every
# provider, local or remote: no legitimate model server is reached through
# 169.254.0.0/16, and that address is the SSRF target that matters most.
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")


class AddressClass(StrEnum):
    """Where a validated base URL points — decided by the address, not the label."""

    # A loopback address (127.0.0.0/8, ::1): the same machine. The only class that is
    # local, and therefore the only one a `LOCAL_ONLY` prompt may reach.
    LOOPBACK = "LOOPBACK"
    # An RFC1918 / IPv6 ULA address: on the operator's network, but *not* this machine.
    # Neither local (it leaves the box) nor public (it is not routable on the internet),
    # so it is refused for both provider kinds — see the module docstring.
    PRIVATE_NETWORK = "PRIVATE_NETWORK"
    # A public, internet-routable address. The only class a remote provider may use.
    REMOTE = "REMOTE"

    @property
    def is_local(self) -> bool:
        """Only loopback is local: the same machine, not merely the same network.

        A private-network address is deliberately *not* local (§2). The distinction the
        privacy class cares about is "does this leave the box?", and only a loopback
        address answers no — a prompt sent to `10.0.0.5` has left the machine, whatever
        the network topology.
        """
        return self is AddressClass.LOOPBACK


@runtime_checkable
class HostResolver(Protocol):
    """Resolves a hostname to its addresses — the one place DNS enters the gate.

    A narrow port so the resolution the SSRF check depends on is injectable: production
    uses `SystemHostResolver` (a real lookup on a thread), and a test injects a
    deterministic fake, so no test contacts real DNS. `resolve` returns the concrete
    addresses a name maps to; the caller validates every one of them.
    """

    async def resolve(self, hostname: str) -> tuple[_IPAddress, ...]:
        ...


class SystemHostResolver:
    """The production resolver: `getaddrinfo` off the event loop, deduplicated.

    Runs the blocking lookup through `loop.getaddrinfo`, which hands it to a thread, so
    the async request path is not blocked. Returns every distinct address a name maps
    to — the caller must accept the answer only if *all* of them pass, so a name that
    returns both a public and a private address is caught (§6). A lookup failure raises
    `OSError`, which the caller normalizes into a typed, text-free `LLMError` (§7).
    """

    async def resolve(self, hostname: str) -> tuple[_IPAddress, ...]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP)
        addresses: list[_IPAddress] = []
        seen: set[str] = set()
        for *_, sockaddr in infos:
            literal = sockaddr[0]
            if literal not in seen:
                seen.add(literal)
                addresses.append(ipaddress.ip_address(literal))
        return tuple(addresses)


@dataclass(frozen=True)
class ValidatedBaseUrl:
    """A base URL that passed the static policy, with what the policy concluded.

    `normalized` is the URL with a trailing slash trimmed, so an adapter appends
    `/chat/completions` without doubling a slash. `address_class` drives the
    `LOCAL_EXECUTION` capability: an adapter sets it from *this*, not from a
    connection's name (§89). For a hostname, `address_class` is `REMOTE` provisionally —
    the concrete addresses are resolved and validated immediately before each request,
    not here (see `needs_runtime_resolution`).
    """

    normalized: str
    scheme: str
    host: str
    address_class: AddressClass

    @property
    def is_local(self) -> bool:
        return self.address_class.is_local

    @property
    def needs_runtime_resolution(self) -> bool:
        """Whether the host is a name whose addresses must be checked before a request.

        True only for a remote provider whose host is not an IP literal: a loopback or
        literal address was fully classified at construction and never needs DNS, and a
        local provider is a loopback literal by construction. A remote *name* is the one
        case whose concrete addresses are unknown until the lookup.
        """
        if self.address_class is not AddressClass.REMOTE:
            return False
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return True
        return False


def _classify_address(address: _IPAddress) -> AddressClass:
    """Classify a concrete IP, refusing the ranges no provider legitimately uses.

    The link-local/metadata, unspecified, multicast and reserved ranges are refused for
    everyone — a model server is never reached through any of them, and they are the
    SSRF payloads that matter. What remains is one of the three classes: loopback
    (same machine), private-network (RFC1918 / IPv6 ULA), or public.
    """
    if address in _LINK_LOCAL_V4 or address.is_link_local:
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL points at a link-local or metadata address")
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL points at an unspecified, multicast or reserved "
                   "address")
    if address.is_loopback:
        return AddressClass.LOOPBACK
    # `is_private` covers the IPv4 RFC1918 ranges and the IPv6 unique-local range
    # (fc00::/7); there is no separate `is_unique_local` on the stdlib address types.
    if address.is_private:
        return AddressClass.PRIVATE_NETWORK
    return AddressClass.REMOTE


def _classify_host(host: str) -> AddressClass | None:
    """Classify a host literal, or `None` when it is a name needing resolution."""
    if host in ("localhost", "localhost.localdomain"):
        # The two explicit loopback aliases (§11): accepted as loopback without a
        # lookup, the one place a label is trusted.
        return AddressClass.LOOPBACK
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname, not a literal
    return _classify_address(address)


def validate_base_url(base_url: str, *, require_local: bool) -> ValidatedBaseUrl:
    """Vet a user-supplied base URL statically, or raise `PROVIDER_MISCONFIGURED`.

    `require_local` is set by a `LOCAL_OPENAI_COMPATIBLE` provider: the address must
    then be a loopback literal (or `localhost`), so a "local" connection cannot be
    pointed off the machine — not at a public host, not at a private-LAN address, not
    at a name. A remote provider (`require_local=False`) demands `https`, and accepts a
    public literal or a hostname; a *private-network or loopback literal* is refused,
    because a remote provider must be public (§4). A hostname is left provisionally
    `REMOTE` and its concrete addresses are validated before each request by
    `resolve_and_validate_remote_host` — the static check cannot resolve without a side
    effect and a TOCTOU window.
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

    address_class = _classify_host(host)  # raises on link-local/metadata/reserved

    if require_local:
        if address_class is not AddressClass.LOOPBACK:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="a local provider must point at a loopback address on this "
                       "machine")
        # A loopback server over plain HTTP is fine — the data never leaves the box.
    else:
        if address_class is None:
            # A hostname for a remote provider: allowed only over https. The concrete
            # address is resolved and validated by the client at call time, not here.
            address_class = AddressClass.REMOTE
        elif address_class is not AddressClass.REMOTE:
            # A loopback or private-network *literal* for a remote provider: refused.
            # Loopback belongs to a local provider; a private-LAN address is neither
            # this machine nor public, so a remote provider must not reach it (§4).
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="a remote provider must point at a public address; configure a "
                       "local provider for a loopback endpoint")
        if parts.scheme != "https":
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="a remote provider must use https so the prompt is encrypted "
                       "in transit")

    return ValidatedBaseUrl(
        normalized=trimmed.rstrip("/"),
        scheme=parts.scheme,
        host=host,
        address_class=address_class)


def validate_resolved_addresses(addresses: Sequence[_IPAddress]) -> None:
    """Refuse unless every resolved address is public — the SSRF answer at call time.

    A name is only as safe as *all* of its answers: a single loopback, private-network,
    link-local or metadata address among them is a refusal (§6), because an attacker
    controlling the name only needs one bad answer to be used. An empty result is a
    misconfiguration, not a silent pass.
    """
    if not addresses:
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL host did not resolve to any address")
    for address in addresses:
        # `_classify_address` raises for the always-illegitimate ranges (link-local,
        # metadata, reserved); a loopback or private-network answer is not those but is
        # still not public, so it too is refused for a remote provider.
        if _classify_address(address) is not AddressClass.REMOTE:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="the base URL host resolved to a non-public address")


async def resolve_and_validate_remote_host(
        host: str, resolver: HostResolver) -> None:
    """Resolve a remote hostname and refuse if any answer is not public (§4, §6, §7).

    Called immediately before an outbound request so the check sees what the request
    will (modulo the residual rebinding window the module docstring notes). A resolver
    failure — NXDOMAIN, a timeout, anything — is normalized to a typed
    `PROVIDER_MISCONFIGURED` whose detail is composed here, never the resolver's own
    exception text, so a `socket.gaierror` string cannot leak into an error (§7, §25).
    """
    try:
        addresses = await resolver.resolve(host)
    except LLMError:
        raise  # `validate_resolved_addresses` already normalized this one
    except Exception as exc:  # any resolver failure normalizes to one typed answer
        raise LLMError(
            LLMFailureCode.PROVIDER_MISCONFIGURED,
            detail="the base URL host could not be resolved") from exc
    validate_resolved_addresses(addresses)
