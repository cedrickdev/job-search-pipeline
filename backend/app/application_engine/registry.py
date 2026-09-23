"""Which adapter serves which channel, and how the engine finds it (§10).

The counterpart of `backend.app.llm.registry`, and it exists for the same rule: a
service must never write `if platform == "greenhouse"`. It asks the registry for the
adapter that serves an `ApplicationChannel` and calls a typed method, so adding a
new ATS is registering an adapter, not editing a switch in business code.

Dispatch is one-adapter-per-channel: a channel names *how* an application reaches an
employer, and exactly one adapter drives each route. A duplicate registration is a
composition-time error, loud on purpose (like the LLM registry's) — two adapters
answering to `ATS_FORM` would make which one runs depend on registration order.

The registry also answers the fallback question §12-13 asks: a channel with no
registered adapter resolves to the generic adapter, which prepares nothing it cannot
and reports `REQUIRES_HUMAN` rather than failing — so an unknown channel degrades to
a human hand-off instead of an exception no user can act on.
"""
from enum import StrEnum

from backend.app.application_engine.contracts import ApplicationAdapter
from backend.app.domain.application_channel import ApplicationChannel


class AdapterRegistryErrorCode(StrEnum):
    """Why a registry call could not be answered — at composition time, never mid-run."""

    DUPLICATE_ADAPTER = "DUPLICATE_ADAPTER"
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"


class AdapterRegistryError(Exception):
    """An adapter-registry contract was broken while composing the engine.

    Loud on purpose: a duplicate channel means two adapters claim one route, and
    which submits an application would then depend on registration order. That is a
    bug to fix before the process runs, not a condition to degrade around — the
    degrade path (an *unregistered* channel) is a first-class outcome, not this.
    """

    def __init__(self, code: AdapterRegistryErrorCode, message: str, *,
                 channel: ApplicationChannel | None = None) -> None:
        self.code = code
        self.channel = channel
        subject = f" [{channel}]" if channel is not None else ""
        super().__init__(f"{code}{subject}: {message}")


class ApplicationAdapterRegistry:
    """The set of registered adapters, keyed by the channel each serves.

    A `fallback` adapter (the generic one, §13) is optional but recommended: with it,
    `resolve` never raises for an unknown channel and instead returns the
    conservative adapter that hands off to a human. Without it, an unknown channel is
    an `UNKNOWN_CHANNEL` error the caller must handle.
    """

    def __init__(self, *, fallback: ApplicationAdapter | None = None) -> None:
        self._adapters: dict[ApplicationChannel, ApplicationAdapter] = {}
        self._fallback = fallback

    def register(self, adapter: ApplicationAdapter) -> None:
        """Add one adapter under the channel its capabilities declare."""
        channel = adapter.capabilities.channel
        if channel in self._adapters:
            raise AdapterRegistryError(
                AdapterRegistryErrorCode.DUPLICATE_ADAPTER,
                "an adapter for this channel is already registered", channel=channel)
        self._adapters[channel] = adapter

    def register_all(self, adapters: tuple[ApplicationAdapter, ...]) -> None:
        for adapter in adapters:
            self.register(adapter)

    def has(self, channel: ApplicationChannel) -> bool:
        return channel in self._adapters

    def get(self, channel: ApplicationChannel) -> ApplicationAdapter:
        """The adapter registered for a channel, or `UNKNOWN_CHANNEL` if none is.

        Exact lookup with no fallback — use `resolve` for the degrade-to-generic
        behaviour §13 wants; this is for a caller that needs to know a specific
        channel is genuinely served.
        """
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise AdapterRegistryError(
                AdapterRegistryErrorCode.UNKNOWN_CHANNEL,
                "no adapter is registered for this channel", channel=channel) from exc

    def resolve(self, channel: ApplicationChannel) -> ApplicationAdapter:
        """The adapter to use for a channel, falling back to the generic one (§12-13).

        A registered adapter if one exists; otherwise the fallback, which prepares
        conservatively and routes to a human. Raises `UNKNOWN_CHANNEL` only when no
        adapter serves the channel *and* no fallback was configured — a deployment
        choice, not a runtime surprise.
        """
        adapter = self._adapters.get(channel)
        if adapter is not None:
            return adapter
        if self._fallback is not None:
            return self._fallback
        raise AdapterRegistryError(
            AdapterRegistryErrorCode.UNKNOWN_CHANNEL,
            "no adapter is registered for this channel and no fallback is configured",
            channel=channel)

    @property
    def channels(self) -> tuple[ApplicationChannel, ...]:
        """Every channel with a registered adapter, in a deterministic order."""
        return tuple(sorted(self._adapters, key=lambda channel: channel.value))
