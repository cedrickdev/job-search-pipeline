"""The form fingerprint: a hash of a form's *structure*, nothing ephemeral (§14, §34, §36).

The fingerprint is what lets the submit-time re-check catch a form that changed under the prepared
answers (§14): PREPARE computes it, the service stores it on the application, and SUBMIT recomputes
it and refuses to fill a form whose hash no longer matches. For that to be trustworthy the hash
must depend on exactly the structural facts and nothing volatile — so a CSRF token, a session id, a
DOM-random wrapper id or a tracking parameter changing between two loads of the *same* form leaves
the fingerprint identical (§14, §36), while adding, removing or re-typing a field changes it.

What goes in: the platform, and for each question its stable key, its kind, whether it is required,
and its option identities (for a choice field). What stays out: labels (human prose that can be
reworded without a structural change), and anything the caller never puts in a question in the
first place — CSRF tokens, random ids, timestamps.
"""
from __future__ import annotations

import hashlib
import json

from backend.app.domain.application_answer import ApplicationQuestion


def compute_form_fingerprint(
        platform: str, questions: tuple[ApplicationQuestion, ...]) -> str:
    """A deterministic hex digest of a form's normalized structure (§14).

    Questions are sorted by key so DOM order does not matter, and each contributes only its
    structural facts. Two loads of the same form — differing only in a CSRF token or a random
    wrapper id the extractor never captured — produce the same digest; a form that gained a
    required field, dropped one, or changed a field's type or options produces a different one.
    """
    normalized = {
        "platform": platform,
        "fields": sorted(
            (
                {
                    "key": question.key,
                    "kind": question.kind.value,
                    "required": question.required,
                    "options": sorted(question.options),
                }
                for question in questions
            ),
            key=lambda field: str(field["key"]),
        ),
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
