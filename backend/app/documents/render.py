"""ATS-safe rendering of a `DocumentContent` to a one-page PDF.

The renderer inherits every hard-won rule from V1's `pipeline.cv_render` and
restates them for the structured V2 document:

- **ATS-readable text, not a picture of text (§28).** The layout is a single
  column of semantic headings and bullet lists — no multi-column tables, no text
  boxes, no images. The extraction test (`tests/test_v2_documents.py`) reads the
  PDF back with `pypdf` and asserts the candidate's facts survive, which is the
  only honest way to prove an ATS can read it.
- **One page (§30).** A résumé that overflows drops its lowest-priority bullet and
  re-renders, exactly as V1 does. "Lowest priority" here is document order: the
  generator emits entries newest-first and bullets most-important-first, so the
  bullet dropped is the last one of the last entry that still has more than one —
  a job is never stripped to zero bullets.
- **Fill floor (§31).** The fraction of the page the content covers is measured
  and reported. Unlike V1 it is advisory rather than fatal: V2's generator selects
  from real evidence and does not over-trim, so a sparse résumé reflects thin
  evidence, not a bug — the service surfaces the number rather than refusing to
  render.

Rendering is deterministic: the same content renders to the same bytes, because
the PDF's producer string and creation date are fixed. That is what lets a
re-render of an unchanged version be a no-op rather than a new artifact, and it is
honest — the producer names this renderer, it does not impersonate Word the way
V1 did to appease a specific ATS.
"""
import html
import io
from dataclasses import dataclass
from typing import Any, TypedDict

import weasyprint.pdf as _wp_pdf
from weasyprint import CSS, HTML

from backend.app.domain.documents import (
    CoverLetterDocument,
    DocumentContent,
    ResumeDocument,
    ResumeEntry,
)


class _EntryLayout(TypedDict):
    """A résumé entry as mutable layout state, so the one-page loop can trim it.

    The frozen `ResumeEntry` is copied into this on the way into rendering; the
    loop drops bullets from `bullets` without touching the domain object the
    service still holds.
    """

    heading: str
    subheading: str | None
    bullets: list[str]

# Stamped on every rendered version's `generator_key`-adjacent provenance and bumped
# when the layout changes, so a PDF's look is traceable to the code that made it.
DOCUMENT_RENDERER_KEY = "ats-weasyprint/1"

# Fixed producer and creation date: deterministic output, and honest — this names
# the renderer rather than impersonating another program.
_PDF_PRODUCER = "career-intelligence-os document renderer"
_PDF_CREATED = "2026-01-01"

# A tailored résumé should cover at least this fraction of the printable page to
# look finished (V1 spec §5). Advisory in V2: reported, not enforced.
FILL_FLOOR = 0.92

_STYLESHEET = """
@page { size: A4; margin: 1.6cm 1.8cm; }
* { box-sizing: border-box; }
body {
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 10.5pt; line-height: 1.35; color: #111; margin: 0;
}
h1 { font-size: 18pt; margin: 0 0 2pt 0; }
.headline { font-size: 11pt; color: #333; margin: 0 0 10pt 0; }
h2 {
  font-size: 11pt; text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid #999; padding-bottom: 2pt; margin: 12pt 0 6pt 0;
}
.summary { margin: 0 0 4pt 0; }
.entry { margin: 0 0 8pt 0; }
.entry-heading { font-weight: bold; margin: 0; }
.entry-subheading { color: #444; margin: 0 0 2pt 0; }
ul { margin: 2pt 0 0 0; padding-left: 16pt; }
li { margin: 0 0 2pt 0; }
.skills p, .languages p { margin: 0 0 3pt 0; }
.letter p { margin: 0 0 8pt 0; }
.letter .signature { margin-top: 12pt; }
"""


@dataclass(frozen=True)
class RenderedDocument:
    """The output of a render: the PDF bytes and what the render decided.

    `dropped` names the bullets removed to fit one page (empty for a cover
    letter, which is never trimmed), and `fill` / `meets_fill_floor` report how
    full the page is so the service can note a sparse résumé without failing.
    """

    pdf_bytes: bytes
    page_count: int
    fill: float
    dropped: tuple[str, ...]
    renderer_key: str = DOCUMENT_RENDERER_KEY

    @property
    def meets_fill_floor(self) -> bool:
        return self.fill >= FILL_FLOOR


def render_document(content: DocumentContent, *, language: str) -> RenderedDocument:
    """Render one document to a one-page ATS PDF.

    `language` is carried for parity with the generator and for a future
    localized label set; the content is already in one language by the time it
    reaches here, so the renderer lays out whatever text it is given.
    """
    if isinstance(content, ResumeDocument):
        return _render_resume(content)
    return _render_cover_letter(content)


def _render_resume(resume: ResumeDocument) -> RenderedDocument:
    # Work on a plain, mutable copy of the bullets so the one-page loop can drop
    # them without touching the frozen domain object the service still holds.
    experience = [_entry_dict(entry) for entry in resume.experience]
    education = [_entry_dict(entry) for entry in resume.education]
    dropped: list[str] = []
    doc = _weasy_doc(_resume_html(resume, experience, education))
    while len(doc.pages) > 1:
        removed = _drop_lowest_priority_bullet(experience)
        if removed is None:
            break  # nothing left to drop; ship it and let the caller see pages>1
        dropped.append(removed)
        doc = _weasy_doc(_resume_html(resume, experience, education))
    return _finish(doc, tuple(dropped))


def _render_cover_letter(letter: CoverLetterDocument) -> RenderedDocument:
    doc = _weasy_doc(_cover_letter_html(letter))
    return _finish(doc, ())


# --- HTML assembly ------------------------------------------------------

def _resume_html(resume: ResumeDocument, experience: list[_EntryLayout],
                 education: list[_EntryLayout]) -> str:
    parts: list[str] = [f"<h1>{_esc(resume.full_name)}</h1>"]
    if resume.headline:
        parts.append(f'<p class="headline">{_esc(resume.headline)}</p>')
    if resume.summary is not None:
        parts.append('<section><h2>Summary</h2>'
                     f'<p class="summary">{_esc(resume.summary.text)}</p></section>')
    if experience:
        parts.append('<section><h2>Experience</h2>'
                     + "".join(_entry_html(entry) for entry in experience)
                     + '</section>')
    if education:
        parts.append('<section><h2>Education</h2>'
                     + "".join(_entry_html(entry) for entry in education)
                     + '</section>')
    if resume.skill_groups:
        rows = []
        for group in resume.skill_groups:
            label = f"<strong>{_esc(group.name)}:</strong> " if group.name else ""
            rows.append(f"<p>{label}{_esc(', '.join(group.skills))}</p>")
        parts.append(f'<section class="skills"><h2>Skills</h2>{"".join(rows)}</section>')
    if resume.languages:
        parts.append('<section class="languages"><h2>Languages</h2>'
                     f'<p>{_esc(", ".join(resume.languages))}</p></section>')
    return _document(parts)


def _entry_html(entry: _EntryLayout) -> str:
    parts = [f'<p class="entry-heading">{_esc(entry["heading"])}</p>']
    if entry["subheading"]:
        parts.append(f'<p class="entry-subheading">{_esc(entry["subheading"])}</p>')
    if entry["bullets"]:
        items = "".join(f"<li>{_esc(text)}</li>" for text in entry["bullets"])
        parts.append(f"<ul>{items}</ul>")
    return f'<div class="entry">{"".join(parts)}</div>'


def _cover_letter_html(letter: CoverLetterDocument) -> str:
    parts: list[str] = ['<section class="letter">']
    if letter.recipient:
        parts.append(f"<p>{_esc(letter.recipient)}</p>")
    if letter.greeting:
        parts.append(f"<p>{_esc(letter.greeting)}</p>")
    parts += [f"<p>{_esc(paragraph.text)}</p>" for paragraph in letter.body]
    if letter.closing:
        parts.append(f"<p>{_esc(letter.closing)}</p>")
    parts.append(f'<p class="signature">{_esc(letter.signature)}</p>')
    parts.append("</section>")
    return _document(parts)


def _document(body_parts: list[str]) -> str:
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
            + "".join(body_parts) + "</body></html>")


def _entry_dict(entry: ResumeEntry) -> _EntryLayout:
    return {
        "heading": entry.heading,
        "subheading": entry.subheading,
        # Each bullet keeps its document order as its priority: the last bullet of
        # the last entry is the lowest-priority one, dropped first.
        "bullets": [bullet.text for bullet in entry.bullets],
    }


def _drop_lowest_priority_bullet(entries: list[_EntryLayout]) -> str | None:
    """Remove one bullet — the last of the last entry that has more than one.

    The structured analogue of V1's priority rule: document order *is* the
    priority (generator emits most-important first), so trimming from the end of
    the last multi-bullet entry drops the least important line while never
    stripping an entry to zero bullets. Returns the dropped text, or `None` when
    every entry is down to a single bullet and nothing more can go.
    """
    for entry in reversed(entries):
        if len(entry["bullets"]) > 1:
            return entry["bullets"].pop()
    return None


# --- WeasyPrint boundary ------------------------------------------------

def _weasy_doc(html_string: str) -> Any:
    # WeasyPrint ships no type stubs, so its `Document` is `Any`; the boundary is
    # kept to these three helpers so the untyped surface does not spread.
    return HTML(string=html_string).render(stylesheets=[CSS(string=_STYLESHEET)])


def _finish(doc: Any, dropped: tuple[str, ...]) -> RenderedDocument:
    buffer = io.BytesIO()
    original = _wp_pdf.VERSION
    _wp_pdf.VERSION = _PDF_PRODUCER
    doc.metadata.generator = _PDF_PRODUCER
    doc.metadata.created = _PDF_CREATED
    try:
        doc.write_pdf(buffer)
    finally:
        _wp_pdf.VERSION = original
    return RenderedDocument(
        pdf_bytes=buffer.getvalue(),
        page_count=len(doc.pages),
        fill=_page_fill(doc.pages[0]),
        dropped=dropped)


def _page_fill(page: Any) -> float:
    """Fraction of the printable area covered by content (V1's `_page_fill`).

    0.0 is an empty page; 1.0 means content reaches the bottom margin. Reads the
    laid-out box tree — the same private API V1 uses and this codebase has
    verified against the installed WeasyPrint.
    """
    page_box = page._page_box
    top = page_box.content_box_y()
    bottom = top
    for box in page_box.descendants():
        if box is page_box:
            continue
        edge = box.border_box_y() + box.border_height()
        if edge > bottom:
            bottom = edge
    return max(0.0, min(1.0, float((bottom - top) / page_box.height)))


def _esc(value: str) -> str:
    """HTML-escape a candidate-supplied string.

    Every piece of text on the page passes through here, so a value containing
    `<` or `&` renders as those characters instead of injecting markup — the
    render-side half of the injection resistance §53 asks for.
    """
    return html.escape(value, quote=False)
