"""Candidate document generation, its truth guard, rendering and storage.

The package Phase 10 adds. Its centre is `CandidateEvidenceGuard`, the V2 port of
V1's `truth_violations` (`pipeline/tailor_io.py`): the one place that decides
whether generated document content states anything the candidate's evidence does
not support. Around it sit the provider-neutral `DocumentGenerator` protocol and
its deterministic reference implementation, the ATS `render_document` renderer,
and the `DocumentArtifactStore` protocol for the rendered PDF.

Import from here rather than the submodules, so an internal reshuffle stays
internal — the same convention `backend.app.matching` follows.
"""
from backend.app.documents.artifacts import (
    ArtifactNotFound,
    DocumentArtifactStore,
    LocalDocumentArtifactStore,
    StoredArtifact,
)
from backend.app.documents.generator import (
    REFERENCE_GENERATOR_KEY,
    DeterministicDocumentGenerator,
    DocumentGenerator,
    InsufficientEvidence,
)
from backend.app.documents.guard import (
    CandidateEvidenceGuard,
    numeric_tokens,
)
from backend.app.documents.llm_generator import (
    LLM_GENERATOR_KEY,
    LLMDocumentGenerator,
)
from backend.app.documents.render import (
    DOCUMENT_RENDERER_KEY,
    RenderedDocument,
    render_document,
)

__all__ = [
    "ArtifactNotFound",
    "CandidateEvidenceGuard",
    "DeterministicDocumentGenerator",
    "DOCUMENT_RENDERER_KEY",
    "DocumentArtifactStore",
    "DocumentGenerator",
    "InsufficientEvidence",
    "LLM_GENERATOR_KEY",
    "LLMDocumentGenerator",
    "LocalDocumentArtifactStore",
    "REFERENCE_GENERATOR_KEY",
    "RenderedDocument",
    "StoredArtifact",
    "numeric_tokens",
    "render_document",
]
