"""The V2 HTTP surface: FastAPI routes over the application services.

Mounted under `/api/v2` on the same application that serves V1, which is what
makes the transition additive: V1's `/api/**` routes keep their paths, their
bodies and their status codes, and nothing here can change them
(docs/ARCHITECTURE.md §5).

This module holds the prefix and nothing else — deliberately. `server.app` imports
`backend.app.api.router` and `backend.app.api.errors`, and both of those import
this constant; an `__init__` that pulled its own submodules in would make that a
cycle, and an application package that imports FastAPI as a side effect of being
named cannot be inspected by a tool that only wants the prefix.

The layering the routes follow:

* a route reads the request, samples the clock once, and calls a service;
* a service owns the use case and takes the instant as an argument;
* `session_scope` owns the transaction, so nothing below the route commits;
* a response model, never a domain aggregate, is what gets serialized — which is
  what makes it structurally impossible to answer with a password hash or a
  session digest.
"""
from typing import Final

API_V2_PREFIX: Final[str] = "/api/v2"
