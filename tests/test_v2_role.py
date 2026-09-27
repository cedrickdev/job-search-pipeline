# tests/test_v2_role.py
"""The role classifier, pinned: deterministic, title-only, and honest about what it cannot place.

`classify_role_family` is the least-thing-that-is-correct alternative to an ML classifier for
one report, so these tests hold its discipline: it reads the title only, precedence is encoded
by table order (a security engineer is SECURITY, not SOFTWARE_ENGINEERING), an unrecognized
title stays unclassified rather than landing in a catch-all, and a `MANUAL` classification must
name a family because a human who corrected a role picked one.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.identifiers import role_classification_id
from backend.app.domain.role import (
    RoleClassification,
    RoleFamily,
    RoleFamilyProvenance,
    classify_role_family,
)
from tests.v2_builders import NOW, OPPORTUNITY, USER


@pytest.mark.parametrize(("title", "family"), [
    ("Senior Software Engineer", RoleFamily.SOFTWARE_ENGINEERING),
    ("Data Engineer", RoleFamily.DATA_AND_ANALYTICS),
    ("Développeur Full Stack", RoleFamily.SOFTWARE_ENGINEERING),
    ("DevOps Engineer", RoleFamily.INFRASTRUCTURE_AND_DEVOPS),
    ("Product Manager", RoleFamily.PRODUCT_MANAGEMENT),
])
def test_recognized_titles_map_to_their_family(title, family):
    assert classify_role_family(title) is family


def test_precedence_is_encoded_by_table_order():
    """A security engineer is SECURITY even though the title also contains 'engineer'."""
    assert classify_role_family("Security Engineer") is RoleFamily.SECURITY
    assert classify_role_family("Data Engineer") is RoleFamily.DATA_AND_ANALYTICS


def test_an_unrecognized_title_stays_unclassified():
    """A wrong family is worse than an honest gap in a report grouped by family."""
    assert classify_role_family("Chief Happiness Wizard") is None


def test_classification_is_case_insensitive_and_title_only():
    assert classify_role_family("SOFTWARE ENGINEER") is RoleFamily.SOFTWARE_ENGINEERING


def a_classification(**overrides):
    fields = {
        "id": role_classification_id(USER, OPPORTUNITY),
        "user_id": USER,
        "opportunity_id": OPPORTUNITY,
        "role_family": RoleFamily.DATA_AND_ANALYTICS,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return RoleClassification(**fields)


def test_the_deterministic_rule_may_leave_a_role_unclassified():
    unclassified = a_classification(
        role_family=None, provenance=RoleFamilyProvenance.DETERMINISTIC_TITLE)
    assert unclassified.role_family is None
    assert unclassified.is_manual is False


def test_a_manual_classification_must_name_a_family():
    """A human who bothered to correct a role picked one — MANUAL cannot be unclassified."""
    with pytest.raises(ValidationError):
        a_classification(role_family=None, provenance=RoleFamilyProvenance.MANUAL)


def test_a_manual_classification_is_flagged_so_the_backfill_leaves_it_alone():
    manual = a_classification(provenance=RoleFamilyProvenance.MANUAL)
    assert manual.is_manual is True


def test_updated_at_may_not_precede_created_at():
    with pytest.raises(ValidationError):
        a_classification(updated_at=NOW.replace(year=2000))
