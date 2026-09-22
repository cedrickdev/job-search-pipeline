"""Composing the adapter registry — the one place concrete adapters are named.

The counterpart of `backend.app.llm.bootstrap`: business code never imports a
specific adapter, so *something* has to wire the set, and it is here, once. The
registry always carries the generic adapter as its fallback (§13), so any channel
with no specific adapter degrades to a human hand-off rather than an error. The
browser and email adapters are registered only when their collaborators — a
`TaskDispatcher` for the browser worker, an `EmailSender` for outgoing mail — are
actually configured; a deployment that has neither still runs, preparing every
application and routing it to a human, which is the cautious default the whole phase
is built around.
"""
from backend.app.application_engine.adapters.browser import BrowserApplicationAdapter
from backend.app.application_engine.adapters.email import (
    EmailApplicationAdapter,
    EmailSender,
)
from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.application_engine.task_dispatcher import TaskDispatcher


def build_application_registry(
    *,
    task_dispatcher: TaskDispatcher | None = None,
    email_sender: EmailSender | None = None,
) -> ApplicationAdapterRegistry:
    """The populated registry for one deployment.

    Generic fallback always; browser and email only when their collaborators exist.
    Keeping the collaborators optional is what lets the API compose a safe registry
    with nothing but the fallback while a worker deployment composes one that can
    actually drive a page — the engine above the registry cannot tell the difference.
    """
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    if task_dispatcher is not None:
        registry.register(BrowserApplicationAdapter(task_dispatcher))
    if email_sender is not None:
        registry.register(EmailApplicationAdapter(email_sender))
    return registry
