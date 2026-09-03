"""Repository contracts and their SQLAlchemy implementations.

`contracts` names what the application needs from storage, in domain terms and
with no SQLAlchemy in sight; `sqlalchemy_repositories` is one way to satisfy it.
The split is what keeps an application service testable without a database and
what makes the dependency direction hold: a matching service imports
`OpportunityRepository`, never a `Session`.

The contracts are `Protocol`s rather than base classes. Nothing has to inherit to
conform, so an in-memory fake in a test is a plain class, and `mypy --strict`
still checks it structurally against the real thing.
"""
