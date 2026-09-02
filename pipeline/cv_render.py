"""Render CV content to a one-page ATS-safe PDF.

One-page rule: render, and while the PDF overflows, drop the lowest-priority
bullet (highest `priority` number; ties broken from the oldest job up) and
re-render.

Usage: .venv/bin/python -m pipeline.cv_render  (renders base CV, both languages)
"""
import copy
from pathlib import Path

import yaml
import weasyprint.pdf as _wp_pdf
from jinja2 import Environment, FileSystemLoader
from weasyprint import CSS, HTML

from pipeline import paths

_PDF_FAKE_PRODUCER = "Microsoft Word"
_PDF_FAKE_DATE = "2026-05-19"


def load_base_cv() -> dict:
    return yaml.safe_load((paths.CV_DIR / "base_cv.yaml").read_text())


def _loc_factory(lang: str):
    def loc(field):
        if isinstance(field, dict) and lang in field:
            return field[lang]
        return field
    return loc


def _render_html(cv: dict, lang: str) -> str:
    env = Environment(loader=FileSystemLoader(paths.CV_DIR), autoescape=True)
    template = env.get_template("template.html")
    return template.render(cv=cv, lang=lang, loc=_loc_factory(lang))


def _render_doc(cv: dict, lang: str):
    html = _render_html(cv, lang)
    css = (paths.CV_DIR / "style.css").read_text()
    return HTML(string=html, base_url=str(paths.CV_DIR)).render(
        stylesheets=[CSS(string=css)]
    )


def _drop_lowest_priority_bullet(cv: dict) -> str | None:
    """Remove one bullet: highest priority number wins; among ties, the bullet
    from the job listed last (oldest). Returns the dropped bullet id."""
    candidate = None  # ((priority, exp_index), exp_index, bullet_index)
    for ei, exp in enumerate(cv["experience"]):
        if len(exp["bullets"]) <= 1:
            continue  # never strip a job to zero bullets
        for bi, b in enumerate(exp["bullets"]):
            key = (b["priority"], ei)
            if candidate is None or key > candidate[0]:
                candidate = (key, ei, bi)
    if candidate is None:
        return None
    _, ei, bi = candidate
    return cv["experience"][ei]["bullets"].pop(bi)["id"]


def _page_fill(page) -> float:
    """Fraction of the printable area's height covered by content on this
    page: 0.0 is an empty page, 1.0 means content reaches the bottom margin
    (spec v2 §5)."""
    page_box = page._page_box
    top = page_box.content_box_y()
    bottom = top
    for box in page_box.descendants():
        if box is page_box:
            continue
        edge = box.border_box_y() + box.border_height()
        if edge > bottom:
            bottom = edge
    return max(0.0, min(1.0, (bottom - top) / page_box.height))


def render_pdf(cv: dict, lang: str, out_path: str | Path) -> dict:
    """Returns {'pages': int, 'dropped': [bullet ids], 'content': final cv dict, 'fill': float}."""
    cv = copy.deepcopy(cv)
    dropped: list[str] = []
    doc = _render_doc(cv, lang)
    while len(doc.pages) > 1:
        bullet_id = _drop_lowest_priority_bullet(cv)
        if bullet_id is None:
            break  # nothing left to drop; ship it and let the caller see pages>1
        dropped.append(bullet_id)
        doc = _render_doc(cv, lang)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _orig_version = _wp_pdf.VERSION
    _wp_pdf.VERSION = _PDF_FAKE_PRODUCER
    doc.metadata.generator = _PDF_FAKE_PRODUCER
    doc.metadata.created = _PDF_FAKE_DATE
    try:
        doc.write_pdf(out_path)
    finally:
        _wp_pdf.VERSION = _orig_version
    return {"pages": len(doc.pages), "dropped": dropped, "content": cv,
            "fill": _page_fill(doc.pages[0])}


if __name__ == "__main__":
    cv = load_base_cv()
    for lang in ("en", "fr"):
        out = paths.CV_VERSIONS_DIR / f"base_{lang}.pdf"
        result = render_pdf(cv, lang, out)
        print(f"{out}  pages={result['pages']}  fill={result['fill']:.2f}"
              f"  dropped={result['dropped']}")
