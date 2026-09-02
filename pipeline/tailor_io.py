"""Tailoring I/O: validate Claude's tailored CV content through five
deterministic gates (truth, glossary, style, one page, fill floor), then
render FR + EN and register both. A failing CV never touches the tracker."""
import argparse
import json
import re
from collections import Counter

import yaml

from pipeline import paths
from pipeline.cv_render import load_base_cv, render_pdf
from pipeline.cv_versions import register_cv_version
from pipeline.db import connect, init_db
from pipeline.glossary import (cv_text, glossary_violations,
                               load_glossary, load_hard_skill_terms)
from pipeline.statuses import set_status
from pipeline.style_check import load_style_rules, style_violations


class TailorError(ValueError):
    """The tailored content failed a validation gate."""


# A tailored CV must cover at least this fraction of the printable page in
# both languages (spec v2 §5).
FILL_FLOOR = 0.92


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")


NUMERIC_TOKEN_RE = re.compile(
    r"[€$£]?\d+(?:[.,]\d+)*"
    r"(?:\s?[KMBkmb](?![A-Za-z]))?"
    r"(?:\s?[€$£%])?")


def numeric_tokens(text: str) -> Counter:
    """Multiset of numeric tokens (spec v2 §4): digit runs with attached
    currency symbols, decimal separators, percent signs and magnitude
    suffixes, e.g. €2.5M, 2,5 M€, 80%, 4. Narrow and no-break spaces are
    normalized first so French number formatting compares equal."""
    text = text.replace("\u202f", " ").replace("\u00a0", " ")
    return Counter(match.group().replace(" ", "").upper()
                   for match in NUMERIC_TOKEN_RE.finditer(text))


def _term_pattern(term: str) -> re.Pattern:
    """Case-insensitive word-boundary pattern for a glossary term, tolerating
    a plural 's' (texts are lowercased before matching)."""
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"s?(?![A-Za-z0-9])")


def _base_tech_corpus(base: dict) -> str:
    """All base library prose a technology may legitimately come from:
    every bullet, the summary, and the skills items (spec v2 §4)."""
    parts = [base["summary"]["en"], base["summary"]["fr"]]
    for exp in base["experience"]:
        for bullet in exp["bullets"]:
            parts += [bullet["en"], bullet["fr"]]
    for group in base["skills"]:
        parts += list(group["items"])
    return " ".join(parts).lower()


def _fact_violations(text: str, allowed_numbers: Counter, corpus: str,
                     terms: list[str], where: str) -> list[str]:
    """Numbers rule and tech rule for one piece of rephrased text."""
    issues = []
    invented = numeric_tokens(text) - allowed_numbers
    if invented:
        issues.append(f"{where}: numbers not in base: "
                      + ", ".join(sorted(invented)))
    lowered = text.lower()
    for term in terms:
        pattern = _term_pattern(term)
        if pattern.search(lowered) and not pattern.search(corpus):
            issues.append(f"{where}: qualification not in base library: {term}")
    return issues


def unused_bullet_ids(base: dict, content: dict) -> list[str]:
    """Base bullet ids the tailored content did not select, in library
    order. Reported on fill-floor failures so the caller knows exactly what
    is available to add."""
    selected = {b.get("id") for exp in content.get("experience", [])
                for b in exp.get("bullets", [])}
    return [b["id"] for exp in base["experience"] for b in exp["bullets"]
            if b["id"] not in selected]


def truth_violations(base: dict, content: dict,
                     terms: list[str] | None = None) -> list[str]:
    """Fact-locked rephrasing (spec v2 §4). Structure, contact, education,
    languages, titles and dates stay verbatim. Bullet and summary text may
    be reworded per offer, but may not introduce a number absent from the
    source bullet (multiset rule, per language) or a claimable qualification
    (technology, certification, spoken language) absent from the base library.

    `terms` overrides the claimable-qualification list, which otherwise comes
    from the profile's cv/glossary.yaml + cv/keywords.yaml."""
    issues: list[str] = []
    if terms is None:
        terms = load_hard_skill_terms()
    corpus = _base_tech_corpus(base)

    base_exp = {e["id"]: e for e in base["experience"]}
    for exp in content.get("experience", []):
        bid = exp.get("id")
        if bid not in base_exp:
            issues.append(f"unknown experience id: {bid}")
            continue
        ref = base_exp[bid]
        for field in ("company", "start", "end"):
            if exp.get(field) != ref.get(field):
                issues.append(f"{bid}: field '{field}' altered")
        if exp.get("title") != ref.get("title"):
            issues.append(f"{bid}: title altered")
        ref_bullets = {b["id"]: b for b in ref["bullets"]}
        for bullet in exp.get("bullets", []):
            bullet_id = bullet.get("id")
            if bullet_id not in ref_bullets:
                issues.append(f"{bid}: unknown bullet id {bullet_id}")
                continue
            ref_b = ref_bullets[bullet_id]
            for lang in ("en", "fr"):
                text = bullet.get(lang, "")
                if not text:
                    issues.append(f"{bid}/{bullet_id}: {lang} text missing")
                    continue
                issues += _fact_violations(
                    text, numeric_tokens(ref_b.get(lang, "")), corpus, terms,
                    f"{bid}/{bullet_id} ({lang})")
            if "priority" not in bullet:
                issues.append(f"{bid}/{bullet_id}: priority missing")

    for lang in ("en", "fr"):
        pool = numeric_tokens(cv_text(base, lang))
        issues += _fact_violations(
            content.get("summary", {}).get(lang, ""), pool, corpus, terms,
            f"summary ({lang})")

    base_skills = {item for group in base["skills"] for item in group["items"]}
    base_group_labels = [group["group"] for group in base["skills"]]
    for group in content.get("skills", []):
        if group.get("group") not in base_group_labels:
            issues.append(f"skill group not in base library: {group.get('group')}")
        for item in group.get("items", []):
            if item not in base_skills:
                issues.append(f"skill not in base library: {item}")

    if content.get("education") != base.get("education"):
        issues.append("education altered")
    if content.get("contact") != base.get("contact"):
        issues.append("contact altered")
    if content.get("languages") != base.get("languages"):
        issues.append("languages altered")

    return issues


def generate(conn, job_id: int, content: dict, diff_summary: str,
             phone_screen_pct: int | None = None) -> dict:
    base = load_base_cv()
    issues = truth_violations(base, content)
    issues += [f"glossary: {v}" for v in glossary_violations(
        cv_text(content, "en"), cv_text(content, "fr"), load_glossary())]
    banned = load_style_rules()
    for lang in ("en", "fr"):
        issues += [f"style ({lang}): {v}"
                   for v in style_violations(cv_text(content, lang), banned)]
    if issues:
        raise TailorError("; ".join(issues))

    row = conn.execute(
        "SELECT j.company, a.id AS application_id FROM jobs j"
        " JOIN applications a ON a.job_id = j.id WHERE j.id = ?",
        (job_id,)).fetchone()
    if row is None:
        raise TailorError(f"no job/application for job {job_id}")

    # Render both languages BEFORE registering anything: no partial state.
    renders: dict[str, dict] = {}
    pdfs: dict[str, str] = {}
    for lang in ("en", "fr"):
        out_path = paths.CV_VERSIONS_DIR / f"{_slug(row['company'])}_{job_id}_{lang}.pdf"
        result = render_pdf(content, lang, out_path)
        if result["pages"] != 1:
            raise TailorError(f"{lang} render is {result['pages']} pages")
        if result["fill"] < FILL_FLOOR:
            unused = unused_bullet_ids(base, content)
            raise TailorError(
                f"{lang} fill {result['fill']:.2f} is below the {FILL_FLOOR}"
                f" floor; unused base bullets:"
                f" {', '.join(unused) if unused else 'none'}")
        renders[lang] = result
        pdfs[lang] = str(out_path)

    cv_version_ids = [
        register_cv_version(conn, job_id, lang, pdfs[lang],
                            renders[lang]["content"], diff_summary=diff_summary,
                            phone_screen_pct=phone_screen_pct)
        for lang in ("en", "fr")
    ]
    set_status(conn, row["application_id"], "Ready to apply",
               source="tailoring", detail=diff_summary)
    return {"job_id": job_id, "pdfs": pdfs, "cv_version_ids": cv_version_ids,
            "dropped": {lang: renders[lang]["dropped"] for lang in renders}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate and render a tailored CV.")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--content", required=True,
                        help="path to the tailored content YAML")
    parser.add_argument("--diff", required=True,
                        help="human-readable summary of changes vs base")
    args = parser.parse_args()

    connection = connect(paths.DB_PATH)
    init_db(connection)
    with open(args.content) as f:
        tailored = yaml.safe_load(f)
    try:
        outcome = generate(connection, args.job_id, tailored, args.diff)
    except TailorError as exc:
        print(json.dumps({"ok": False, "issues": str(exc)}))
        raise SystemExit(1)
    print(json.dumps({"ok": True, **outcome}))
