"""JD gap analysis: compute keyword coverage score between a job description
and the base CV library. No API needed — pure keyword matching.

Usage:
  python -m pipeline.gap_analysis --job-id N
  python -m pipeline.gap_analysis --text "paste JD text here"
"""
import argparse
import json
import re
from dataclasses import dataclass, field

from pipeline.cv_render import load_base_cv
from pipeline.glossary import load_keywords
from pipeline import paths
from pipeline.db import connect, init_db

GREEN_THRESHOLD = 0.80
YELLOW_THRESHOLD = 0.60


@dataclass
class GapReport:
    required_keywords: list[str]
    matched_keywords: list[str]
    missing_keywords: list[str]
    coverage_score: float
    risk_tier: str  # GREEN | YELLOW | RED
    base_corpus_terms: list[str] = field(default_factory=list)


def _build_base_corpus(base: dict) -> str:
    """All technology terms present in the base library (bullets + skills)."""
    corpus_text_parts = []
    for exp in base["experience"]:
        for bullet in exp["bullets"]:
            corpus_text_parts.append(bullet.get("en", ""))
            corpus_text_parts.append(bullet.get("fr", ""))
    for group in base["skills"]:
        corpus_text_parts.extend(group.get("items", []))
    corpus_text_parts.append(base.get("summary", {}).get("en", ""))
    corpus_text_parts.append(base.get("summary", {}).get("fr", ""))
    corpus = " ".join(corpus_text_parts).lower()
    return corpus


def _term_pattern(term: str) -> re.Pattern:
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"s?(?![A-Za-z0-9])")


def _term_universe(base: dict, terms: list[str] | None) -> list[str]:
    """Every term gap analysis is allowed to notice.

    The curated taxonomy (cv/keywords.yaml) plus whatever the base CV lists as a
    skill: a skill the user actually claims must be detectable in an offer even
    if nobody thought to add it to the taxonomy. First occurrence wins so the
    report order stays stable.
    """
    candidates = list(terms if terms is not None else load_keywords())
    candidates += [item for group in base.get("skills", [])
                   for item in group.get("items", [])]
    seen: set[str] = set()
    unique: list[str] = []
    for term in candidates:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def analyse(jd_text: str, base: dict | None = None,
            terms: list[str] | None = None) -> GapReport:
    """Compute keyword coverage of the JD against the base CV library.

    The term universe is the JD keyword taxonomy (cv/keywords.yaml) unioned with
    the base CV's own skills — NOT the translation glossary, which is a much
    smaller list kept for a different purpose and which used to make this
    function report GREEN on every posting. Pass `terms` to override it.
    """
    if base is None:
        base = load_base_cv()

    universe = _term_universe(base, terms)
    corpus = _build_base_corpus(base)
    jd_lower = jd_text.lower()

    required: list[str] = []
    matched: list[str] = []
    missing: list[str] = []
    in_base: list[str] = []

    for term in universe:
        pattern = _term_pattern(term)
        present_in_base = bool(pattern.search(corpus))
        if present_in_base:
            in_base.append(term)
        if pattern.search(jd_lower):
            required.append(term)
            if present_in_base:
                matched.append(term)
            else:
                missing.append(term)

    total = len(required)
    score = len(matched) / total if total > 0 else 1.0

    if score >= GREEN_THRESHOLD:
        tier = "GREEN"
    elif score >= YELLOW_THRESHOLD:
        tier = "YELLOW"
    else:
        tier = "RED"

    return GapReport(
        required_keywords=required,
        matched_keywords=matched,
        missing_keywords=missing,
        coverage_score=round(score, 3),
        risk_tier=tier,
        base_corpus_terms=in_base,
    )


def analyse_job(conn, job_id: int) -> GapReport | None:
    """Fetch job description from DB and run gap analysis."""
    row = conn.execute(
        "SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or not row["description"]:
        return None
    return analyse(row["description"])


def _format_report(report: GapReport, job_id: int | None = None) -> str:
    lines = []
    if job_id:
        lines.append(f"Gap analysis for job {job_id}")
    lines.append(f"Risk tier: {report.risk_tier}  "
                 f"Coverage: {report.coverage_score:.0%} "
                 f"({len(report.matched_keywords)}/{len(report.required_keywords)} keywords)")
    if report.matched_keywords:
        lines.append(f"Matched:  {', '.join(report.matched_keywords)}")
    if report.missing_keywords:
        lines.append(f"Missing:  {', '.join(report.missing_keywords)}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JD keyword gap analysis.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-id", type=int, help="Job ID to fetch from DB")
    group.add_argument("--text", help="JD text passed directly")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    if args.job_id:
        conn = connect(paths.DB_PATH)
        init_db(conn)
        report = analyse_job(conn, args.job_id)
        conn.close()
        if report is None:
            print(f"No description for job {args.job_id}")
            raise SystemExit(1)
        if args.json:
            import dataclasses
            print(json.dumps(dataclasses.asdict(report), indent=2))
        else:
            print(_format_report(report, args.job_id))
    else:
        report = analyse(args.text)
        if args.json:
            import dataclasses
            print(json.dumps(dataclasses.asdict(report), indent=2))
        else:
            print(_format_report(report))
