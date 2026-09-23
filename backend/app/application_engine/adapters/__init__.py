"""The concrete application adapters, one per channel the engine can drive.

Each module here implements `ApplicationAdapter` for one `ApplicationChannel` and
nothing else knows how that channel works: the browser adapter drives a page through
a `TaskDispatcher`, the email adapter hands a message to an `EmailSender`, and the
generic adapter is the honest floor — it prepares what materials it can and always
hands off to a human (§13). Composition (`bootstrap`) wires the set into an
`ApplicationAdapterRegistry`; business code never imports a specific adapter.
"""
