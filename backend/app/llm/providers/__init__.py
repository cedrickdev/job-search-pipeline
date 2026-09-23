"""LLM provider adapters: the only modules that know a transport.

Each adapter turns the provider-neutral `LLMRequest` into what one provider needs —
a CLI argv, an HTTP body — and its answer back into an `LLMResponse` or a stream of
`LLMStreamEvent`s. Nothing above this package names a provider; `bootstrap` builds
the registry from these, and the router filters over whatever it holds
(docs/LLM_PROVIDER_ARCHITECTURE.md §3).
"""
