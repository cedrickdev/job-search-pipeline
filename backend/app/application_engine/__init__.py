"""The application engine: ports and adapters that turn intent into a submission.

This package is the Phase 12 counterpart of `backend.app.llm`: the *seam* between
the deterministic services that decide whether and how to apply and the concrete,
channel-specific machinery that actually reads a form, fills it and submits it. The
domain (`backend.app.domain.application*`, `execution_gate`) holds the rules; the
services (`backend.app.services.applications`) orchestrate; and this package defines
the contract every adapter meets (`contracts.ApplicationAdapter`), the registry that
dispatches on a typed `ApplicationChannel` rather than a platform string
(`registry`), and the `TaskDispatcher` port that keeps the browser worker behind an
interface (`task_dispatcher`).

The rule that shapes all of it (CLAUDE.md, docs/APPLICATION_ENGINE.md §8): no vendor
branching in business code. A service never writes `if platform == "greenhouse"`; it
asks the registry for the adapter that serves a channel and calls a typed method.
Playwright, an ATS API client and an SMTP sender are all adapters behind the same
`ApplicationAdapter` shape, and the engine above them cannot tell which it holds.
"""
