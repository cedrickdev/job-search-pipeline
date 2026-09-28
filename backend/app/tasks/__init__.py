"""Background execution runtime — the provider-neutral task seam (Phase 16 §32-40).

This package is the part of background execution that knows nothing of Redis, ARQ or
subprocesses: the `TaskDispatcher` seam services enqueue through (§32), the durable
`PersistedTaskDispatcher` that writes a `TaskRun` to the queue table, the `RetryPolicy`
that turns a failure class into a backoff (§38), and the `TaskWorker` runtime that leases,
drives and recovers task runs (§34, §40). The one concrete queue library lives in
`backend.app.infrastructure.tasks`; everything here depends only on the domain and the
`TaskRunRepository` contract, so the queue technology can change without touching a service.
"""
