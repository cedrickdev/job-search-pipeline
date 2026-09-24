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
- `documents` — the ATS résumé and cover letter, their versions and truth guard;
- `search` — saved searches and geographic areas;
- `geo` — geographic search, geocoding and remote semantics;
- `matching` — multidimensional compatibility scoring;
- `eligibility` — binary gates, kept apart from scoring on purpose;
- `policy` — the user's standing rules about applying;
- `decision` — where fit, legality and policy meet;
- `application_channel` — the route an application takes and how far it is trusted;
- `application_failure` — normalized, secret-free execution failure codes;
- `application_answer` — form questions, proposed answers and their resolution;
- `application` — the execution aggregate and its lifecycle state machine;
- `application_event` — the append-only audit trail and submission attempts;
- `execution_gate` — the deterministic gate that authorizes a submission;
- `chat` — the career chat's typed action grammar and its persisted turns;
- `interview` — the adaptive interview simulator: sessions, questions, answers,
  evaluations and deterministic, app-derived readiness.
"""
