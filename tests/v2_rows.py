# tests/v2_rows.py
"""ORM rows for the two tables the persistence tests need but no repository owns.

`users` and `candidate_profiles` are the foreign-key targets of almost everything
Phase 2 stores, and revision 0003 made three of their columns NOT NULL: an account
without an email or a credential is not an account, and a profile with no display
name is a row nothing can render. That turned "add a user row" into four details
that two test modules would each have invented, so they are spelled once here —
valid rather than minimal, and satisfying the CHECKs instead of working around them.

Kept out of `v2_builders.py`, which constructs domain objects: a test about a frozen
Pydantic model has no reason to import the ORM. Kept out of `conftest.py` because
these are constructors and not fixtures — a cross-user test needs two of them, with
different ids, in the same session.
"""
from typing import Any
from uuid import UUID

from backend.app.infrastructure.database.models import CandidateProfileRow, UserRow
from tests.v2_builders import PROFILE, USER

# Never verified by anything: no test here logs in, and `check_password` is exercised
# against real hashes in `test_v2_authentication.py`. Syntactically a real Argon2id
# encoding all the same, so a reader of a failing row does not have to wonder whether
# the credential column holds something that could work. The name avoids `password`
# so that ruff's S105 does not read a fixture as a leaked secret.
UNUSABLE_HASH = ("$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzb21lc2E$"
                 "3RCPqf7xJdWiXPHRRXR2Bg")


def an_email_for(user_id: UUID) -> str:
    """A normalized address derived from the id, so two rows cannot collide.

    `UNIQUE (email)` is real, and a builder with one default address would make the
    second `a_user_row(id=OTHER_USER)` in a cross-user test fail on the unique index
    rather than on what the test is about. Deriving it means uniqueness follows from
    the ids the test already chose, and the address stays greppable in a failure.

    Lower-cased and `@`-containing, because `ck_users_email_normalized` requires
    both — the same shape `normalize_email` produces in the domain.
    """
    return f"user-{user_id}@example.test"


def a_user_row(**overrides: Any) -> UserRow:
    """One account, valid by every constraint `users` carries."""
    columns: dict[str, Any] = {"id": USER, "display_name": "owner",
                               "password_hash": UNUSABLE_HASH}
    columns.update(overrides)
    columns.setdefault("email", an_email_for(columns["id"]))
    return UserRow(**columns)


def a_candidate_profile_row(**overrides: Any) -> CandidateProfileRow:
    """One profile, owned by `USER` unless the test says otherwise.

    No location and no availability: all of those columns are nullable, and an
    all-NULL group is how the schema spells "not stated". A test that needs a
    located profile passes the `location_*` columns it is asserting on.
    """
    columns: dict[str, Any] = {"id": PROFILE, "user_id": USER,
                               "display_name": "Candidate"}
    columns.update(overrides)
    return CandidateProfileRow(**columns)
