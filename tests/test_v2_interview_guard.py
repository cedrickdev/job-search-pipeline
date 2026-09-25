# tests/test_v2_interview_guard.py
"""La barrière de vérité du coaching : la prose ne peut inventer aucun fait (§40-44).

Le coaching de l'entretien — forces, axes de progrès, notes, réponse suggérée, titre et axes
d'un résumé de séance — parle *du* candidat sans jamais porter de citation, contrairement à
une ligne de CV. `InterviewCoachingGuard` réutilise donc telle quelle la barrière Phase 10 :
un nombre ou une compétence revendicable absents du corpus de preuves du candidat sont
rejetés ici exactement comme ils le seraient sur un CV généré. Le garde est pur et
déterministe, si bien que le service l'exécute *avant* toute persistance.
"""
from backend.app.domain.common import SkillRequirement
from backend.app.domain.documents import DocumentViolationCode
from backend.app.interview.guard import InterviewCoachingGuard
from tests.v2_builders import (
    a_candidate_profile,
    an_answer_evaluation,
    an_evidence_record,
    an_interview_session_summary,
    an_opportunity,
)


def _profile():
    """A candidate whose one evidence record mentions the number 30% and no skill term."""
    return a_candidate_profile(evidence=(an_evidence_record(),))


def _opportunity():
    """A posting that *asks for* Kafka — so 'Kafka' is a claimable term, unbacked by evidence."""
    return an_opportunity(skill_requirements=(SkillRequirement(skill="Kafka"),))


def _codes(report):
    return {violation.code for violation in report.violations}
def test_clean_evaluation_prose_clears_the_guard():
    """Generic coaching that asserts no candidate fact passes whole."""
    guard = InterviewCoachingGuard()
    report = guard.review_evaluation(an_answer_evaluation(),
                                     profile=_profile(), opportunity=_opportunity())
    assert report.ok is True
    assert report.violations == ()


def test_a_suggested_answer_inventing_a_number_is_rejected():
    """A drafted answer citing a figure the evidence never carries fails `INVENTED_NUMBER`."""
    guard = InterviewCoachingGuard()
    evaluation = an_answer_evaluation(
        suggested_answer="J'ai réduit la latence de 55% en un trimestre.")
    report = guard.review_evaluation(evaluation,
                                     profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.INVENTED_NUMBER in _codes(report)


def test_a_number_the_evidence_supports_passes():
    """A figure that appears in the candidate's own evidence is truthful, not invented."""
    guard = InterviewCoachingGuard()
    evaluation = an_answer_evaluation(
        suggested_answer="Reprenez l'exemple où vous avez réduit la latence de 30%.")
    report = guard.review_evaluation(evaluation,
                                     profile=_profile(), opportunity=_opportunity())
    assert report.ok is True


def test_a_strength_inventing_a_claimable_skill_is_rejected():
    """Praising a skill the posting wants but the evidence never shows fails `INVENTED_TERM`."""
    guard = InterviewCoachingGuard()
    evaluation = an_answer_evaluation(strengths=("bonne maîtrise de Kafka",))
    report = guard.review_evaluation(evaluation,
                                     profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.INVENTED_TERM in _codes(report)


def test_the_guard_collects_every_violation_at_once():
    """A rejected payload reports all its problems, not just the first — like the document guard."""
    guard = InterviewCoachingGuard()
    evaluation = an_answer_evaluation(
        strengths=("maîtrise de Kafka",),
        suggested_answer="Vous avez livré en réduisant les coûts de 40%.")
    report = guard.review_evaluation(evaluation,
                                     profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert {DocumentViolationCode.INVENTED_TERM,
            DocumentViolationCode.INVENTED_NUMBER} <= _codes(report)


def test_a_clean_summary_clears_and_a_fabricated_one_is_rejected():
    """A summary's headline, strengths and focus areas run the same gate as an evaluation."""
    guard = InterviewCoachingGuard()
    clean = guard.review_summary(an_interview_session_summary(),
                                 profile=_profile(), opportunity=_opportunity())
    assert clean.ok is True

    fabricated = an_interview_session_summary(
        focus_areas=("approfondir Kafka pour ce poste",))
    report = guard.review_summary(fabricated,
                                  profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.INVENTED_TERM in _codes(report)


# --- a posting fact is not a candidate fact (§10-17) --------------------------
#
# The question gate widens the allowed corpus with the posting, so a question may *reference*
# a skill or a figure the posting states. What it must still refuse is attributing that
# posting-only fact to the candidate as their own experience. These prove both directions for
# a skill term (Kafka) and a number (a team of 20): a role reference or a hypothetical passes,
# a possessive or a second-person assertion of experience is rejected.


def _team_opportunity():
    """A posting whose description states a team of 20 — so '20' is the posting's figure.

    Kafka stays a sought skill too, so both a posting-only term and a posting-only number are
    available; neither appears in the candidate's evidence (which mentions only 30%)."""
    return an_opportunity(
        skill_requirements=(SkillRequirement(skill="Kafka"),),
        description="The platform team has 20 engineers.")


def test_a_question_referencing_a_posting_skill_as_a_role_fact_passes():
    """'The role mentions Kafka. How would you approach it?' — a role reference, not a claim."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "The role mentions Kafka. How would you approach using it?",
        profile=_profile(), opportunity=_opportunity())
    assert report.ok is True


def test_a_hypothetical_second_person_question_about_a_posting_skill_passes():
    """'How would you use Kafka?' carries 'you use' but is hypothetical — approach, not a claim."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "How would you use Kafka in this role?",
        profile=_profile(), opportunity=_opportunity())
    assert report.ok is True


def test_a_question_attributing_a_posting_skill_to_the_candidate_is_rejected():
    """'Tell me about your extensive Kafka experience' pins a posting-only skill on the candidate."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "Tell me about your extensive Kafka experience.",
        profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.MISATTRIBUTED_TO_CANDIDATE in _codes(report)


def test_a_question_referencing_a_posting_number_as_a_role_fact_passes():
    """'The role involves a team of 20' states the posting's own figure, not the candidate's."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "The role involves a team of 20 engineers. How would you organise it?",
        profile=_profile(), opportunity=_team_opportunity())
    assert report.ok is True


def test_a_question_attributing_a_posting_number_to_the_candidate_is_rejected():
    """'You managed a team of 20' credits the candidate with a figure only the posting states."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "You managed a team of 20 engineers.",
        profile=_profile(), opportunity=_team_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.MISATTRIBUTED_TO_CANDIDATE in _codes(report)


def test_a_possessive_over_a_posting_number_is_rejected():
    """'Your team of 20' binds a posting-only figure to the candidate through the possessive."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "Walk me through your team of 20 and how you structured it.",
        profile=_profile(), opportunity=_team_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.MISATTRIBUTED_TO_CANDIDATE in _codes(report)


def test_a_question_inventing_a_number_from_nowhere_still_fails_the_grounding_gate():
    """A figure in neither the candidate nor the posting is a fabrication — the original gate, intact."""
    guard = InterviewCoachingGuard()
    report = guard.review_question(
        "How would you replicate cutting costs by 73% here?",
        profile=_profile(), opportunity=_opportunity())
    assert report.ok is False
    assert DocumentViolationCode.INVENTED_NUMBER in _codes(report)

