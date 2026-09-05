"""Application services: the use cases, orchestrated over repository Protocols.

A service holds no session, no engine and no request. It takes repositories as
`Protocol`s and the current instant as an argument, which is what makes the
authentication flow testable with in-memory fakes and a frozen clock — the login
lockout, session expiry and CSRF checks in `tests/` never open a socket.

The rule that keeps this layer honest: **the caller owns the clock and the
transaction.** Every method that needs "now" is given it, and nothing here
commits. `backend.app.api` supplies both, from the request and from
`session_scope`.
"""
