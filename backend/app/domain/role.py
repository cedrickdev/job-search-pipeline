"""`RoleClassification` — the smallest honest answer to "what kind of role is this?" (§18).

Phase 15 wants to report a candidate's funnel *by role family* — "your data roles convert
at twice the rate of your management applications" — but the V2 `Opportunity` has no such
field, and inventing an ML classifier for one report would be exactly the "self-modifying
scoring" the phase forbids. So this module does the least thing that is still correct: a
small, closed `RoleFamily` vocabulary, a deterministic keyword rule that assigns one from a
posting's title, and an explicit provenance so a reader can always tell a machine guess from
a human's correction.

Three rules keep it honest:

- **Unknown stays unknown.** The classifier returns `None`, never a catch-all bucket, when
  no rule matches. A role the platform cannot recognise is reported as "unclassified", not
  quietly folded into "Other", because a bucket that absorbs every failure would make the
  by-role numbers lie (the same discipline `OpportunityType` keeps).
- **Deterministic or manual, never inferred-and-forgotten.** A classification is either the
  output of the pure `classify_role_family` rule (`DETERMINISTIC_TITLE`) or a human's
  explicit choice (`MANUAL`), and it says which. A manual choice outranks the rule: the
  backfill never overwrites a `MANUAL` classification.
- **No LLM, no prose parsing.** The rule is a keyword table over the *title*, reproducible
  and auditable; it never reads the free-text body and never calls a provider (§47).

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only.
"""
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from backend.app.domain.base import DomainModel, UtcDatetime
from backend.app.domain.identifiers import (
    OpportunityId,
    RoleClassificationId,
    UserId,
)


class RoleFamily(StrEnum):
    """A small, closed family of roles — the grouping axis for by-role analytics (§18-20).

    Deliberately coarse. The point is not a taxonomy of every job title but a handful of
    families broad enough that each accumulates a meaningful sample and narrow enough that
    "your data applications convert differently from your engineering ones" is a real
    statement. Country- and language-neutral: the words a board prints map onto these
    members, they do not extend them. A title the keyword rule cannot place stays
    *unclassified* (`None`), never a catch-all member.
    """

    SOFTWARE_ENGINEERING = "SOFTWARE_ENGINEERING"
    DATA_AND_ANALYTICS = "DATA_AND_ANALYTICS"
    INFRASTRUCTURE_AND_DEVOPS = "INFRASTRUCTURE_AND_DEVOPS"
    SECURITY = "SECURITY"
    PRODUCT_MANAGEMENT = "PRODUCT_MANAGEMENT"
    DESIGN = "DESIGN"
    PROJECT_AND_PROGRAM = "PROJECT_AND_PROGRAM"
    IT_SUPPORT = "IT_SUPPORT"
    SALES = "SALES"
    MARKETING = "MARKETING"
    CUSTOMER_SUCCESS = "CUSTOMER_SUCCESS"
    OPERATIONS = "OPERATIONS"
    FINANCE = "FINANCE"
    HUMAN_RESOURCES = "HUMAN_RESOURCES"


class RoleFamilyProvenance(StrEnum):
    """How a role classification was decided — machine rule or human choice (§18).

    `DETERMINISTIC_TITLE` is the keyword rule's output; `MANUAL` is a user's explicit
    correction. The distinction has teeth: the deterministic backfill refuses to overwrite a
    `MANUAL` classification, so a human's judgement is never silently undone by the next
    analytics run.
    """

    DETERMINISTIC_TITLE = "DETERMINISTIC_TITLE"
    MANUAL = "MANUAL"


# The deterministic keyword table, most specific first. Each family maps to the lower-cased
# substrings whose presence in a title assigns it; the first family with any match wins, so
# order encodes precedence — "security engineer" is SECURITY, not SOFTWARE_ENGINEERING, and
# "data engineer" is DATA_AND_ANALYTICS. The rule reads titles only (§47): it is a reviewable
# table, not a model, and a title that matches nothing is left unclassified on purpose.
_TITLE_KEYWORDS: tuple[tuple[RoleFamily, tuple[str, ...]], ...] = (
    (RoleFamily.SECURITY,
     ("security", "sécurité", "infosec", "appsec", "pentest", "soc analyst")),
    (RoleFamily.DATA_AND_ANALYTICS,
     ("data engineer", "data scientist", "data analyst", "analytics", "machine learning",
      "ml engineer", "données", "bi ", "business intelligence")),
    (RoleFamily.INFRASTRUCTURE_AND_DEVOPS,
     ("devops", "sre", "site reliability", "platform engineer", "infrastructure",
      "cloud engineer", "systems engineer", "sysadmin")),
    (RoleFamily.PRODUCT_MANAGEMENT,
     ("product manager", "product owner", "chef de produit", "product lead")),
    (RoleFamily.DESIGN,
     ("designer", "ux", "ui ", "product design", "graphiste", "design lead")),
    (RoleFamily.PROJECT_AND_PROGRAM,
     ("project manager", "programme manager", "program manager", "chef de projet",
      "scrum master", "delivery manager")),
    (RoleFamily.IT_SUPPORT,
     ("support technician", "helpdesk", "help desk", "it support", "technicien",
      "service desk")),
    (RoleFamily.SOFTWARE_ENGINEERING,
     ("software engineer", "développeur", "developer", "programmeur", "full stack",
      "fullstack", "backend", "frontend", "front-end", "back-end", "software developer",
      "ingénieur logiciel", "swe")),
    (RoleFamily.SALES,
     ("sales", "account executive", "business development", "vente", "commercial")),
    (RoleFamily.MARKETING,
     ("marketing", "growth", "seo", "content manager", "communication")),
    (RoleFamily.CUSTOMER_SUCCESS,
     ("customer success", "customer support", "account manager", "client success")),
    (RoleFamily.FINANCE,
     ("finance", "accountant", "comptable", "controller", "contrôleur", "treasury")),
    (RoleFamily.HUMAN_RESOURCES,
     ("human resources", "recruiter", "recruteur", "talent acquisition", "ressources humaines",
      "people ops", "hr ")),
    (RoleFamily.OPERATIONS,
     ("operations", "opérations", "supply chain", "logistics", "logistique")),
)


def classify_role_family(title: str) -> RoleFamily | None:
    """The role family a posting title falls in, or `None` when no rule matches (§18).

    Pure and deterministic: lower-cases the title once and returns the first family whose
    keyword table it hits, precedence encoded by order. Returns `None` — *unclassified* —
    rather than guessing when nothing matches, because a wrong family is worse than an
    honest gap in a report grouped by family. Reads the title only; it never touches the
    posting body and never calls a provider, so the same title always yields the same family
    and the classification is auditable by reading `_TITLE_KEYWORDS` (§47).
    """
    haystack = f" {title.casefold()} "
    for family, keywords in _TITLE_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return family
    return None


class RoleClassification(DomainModel):
    """One user's role-family verdict on one opportunity — deterministic or corrected (§18).

    User-owned and keyed on `(user_id, opportunity_id)` so the derived id makes re-classifying
    idempotent and a manual correction updates the one row rather than adding a second. It
    carries the `role_family` (nullable — an unclassified role is a real, honest state) and
    the `provenance` that decided it. The invariant ties the two together: a `DETERMINISTIC_
    TITLE` classification may be `None` (the rule matched nothing), but a `MANUAL` one must
    name a family, because a human who bothered to correct a role picked one.
    """

    id: RoleClassificationId
    user_id: UserId
    opportunity_id: OpportunityId
    role_family: RoleFamily | None = None
    provenance: RoleFamilyProvenance = RoleFamilyProvenance.DETERMINISTIC_TITLE
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _manual_names_a_family_and_timestamps_are_coherent(self) -> Self:
        if self.provenance is RoleFamilyProvenance.MANUAL and self.role_family is None:
            raise ValueError(
                "a MANUAL role classification must name a role_family; only the "
                "deterministic rule may leave a role unclassified")
        if self.updated_at < self.created_at:
            raise ValueError("RoleClassification updated_at must not precede created_at")
        return self

    @property
    def is_manual(self) -> bool:
        """Whether a human set this classification — the backfill must not overwrite it."""
        return self.provenance is RoleFamilyProvenance.MANUAL
