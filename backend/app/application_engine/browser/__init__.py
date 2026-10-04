"""Real Playwright application automation, behind the browser worker (Phase 12.1).

Everything Playwright-specific lives under this package, and nothing else imports it: the
`browser_worker` entrypoint composes it, the `SubprocessTaskDispatcher` runs that worker in an
isolated container (§30), and the API and general-lane worker never load a browser at all.

The split mirrors the phase's rules. A `BrowserApplicationDriver` (`drivers/`) knows *how* one ATS
works — its selectors, its confirmation page — and nothing about *whether* to apply (§47). The
`execution` orchestrator is driver-agnostic and owns the safety it would be unsafe to leave to each
driver: it compares the form fingerprint before filling (§14), refuses a required field with no
trustworthy answer (§11), clicks the one irreversible control at most once (§20), and reports an
unconfirmed send as STATE_UNKNOWN rather than a guess (§21-22). The driver adds no authority; the
outer `ApplicationService.submit` has already re-checked policy, eligibility, approval and the
commercial quota before a SUBMIT task is ever dispatched (§1, §19, §45-46).
"""
