"""Shared helpers for source modules."""
from bs4 import BeautifulSoup

from pipeline.jobs import JOB_COLUMNS


def normalize(**fields) -> dict:
    """A job dict with every JOB_COLUMNS key; unspecified fields default to None."""
    unknown = set(fields) - set(JOB_COLUMNS)
    if unknown:
        raise ValueError(f"normalize() got unknown columns: {sorted(unknown)}")
    job = {column: None for column in JOB_COLUMNS}
    job.update(fields)
    return job


def strip_html(markup: str) -> str:
    return BeautifulSoup(markup or "", "html.parser").get_text(" ", strip=True)


def extract_json_object(text: str, start: int) -> str | None:
    """The balanced-brace JSON object beginning at `text[start]` (must be '{').

    For scraping SSR state blobs embedded as `window.SOME_VAR = {...};` in a
    <script> tag, where the object is too large/nested for a regex to safely
    capture. Brace-counting skips braces inside string literals so quoted
    JS/JSON content (e.g. a job description containing '{') doesn't break it.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None
