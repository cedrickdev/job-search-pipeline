"""Docstring-only package marker for the V2 domain layer.

There is no barrel here, and that is a decision rather than an omission. The
strict mypy tier sets `no_implicit_reexport`, so a barrel would have to repeat
every public name twice — once in an import, once in `__all__` — and the second
list is the one that silently rots. Importing from the defining module instead
keeps the dependency direction visible on every import line:

    from backend.app.domain.opportunity import Opportunity, OpportunityType

The modules, in dependency order:

- `base` — the frozen base model and the validated scalar aliases;
- `identifiers` — typed UUID ids and their factories;
- `user` — accounts and server-side sessions;
- `common` — value objects shared by several entities (`Location`, `Reason`, …);
- `opportunity` — the central abstraction and its source record;
- `company` — employers and their physical sites;
- `candidate` — profile, evidence and claims;
- `search` — saved searches and geographic areas;
- `geo` — geographic search, geocoding and remote semantics;
- `matching` — multidimensional compatibility scoring;
- `eligibility` — binary gates, kept apart from scoring on purpose;
- `policy` — the user's standing rules about applying;
- `decision` — where fit, legality and policy meet.
"""
