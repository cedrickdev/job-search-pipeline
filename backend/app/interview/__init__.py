"""The adaptive interview simulator — the application layer of Phase 14.

The domain (`backend.app.domain.interview`) holds the entities, the closed vocabularies and
the deterministic readiness aggregation; this package holds the *services* that drive a
session and the adapters they lean on, mirroring how `backend.app.chat` sits over the chat
domain and `backend.app.documents` over the document domain.

The modules, in dependency order:

- `prompts` — the six versioned, model-independent interview prompts and their structured
  schemas; the evaluation schema is pointedly free of any readiness field (§33);
- `transcriber` — the `SpeechTranscriber` protocol, a deterministic fake and the local
  whisper.cpp adapter that transcribes then discards the audio (§14-17);
- `llm` — the provider-neutral `InterviewLLM` adapter: it routes a prompt, re-validates the
  answer against the domain, and never lets a provider author readiness (§45-48);
- `guard` — the interview coaching truth gate, reusing the Phase 10 evidence guard (§40-44);
- `engine` — the `InterviewQuestionEngine`: bounds, evaluation-before-next-question, the
  follow-up decision and difficulty adaptation (§26-32, §64-72);
- `context` — the minimal, task-specific `InterviewContextBuilder` (§60);
- `service` — the `InterviewService` lifecycle over the repositories (§1-6, §54-59).

Every truth boundary the phase rests on is physical here, not merely documented: readiness
is computed by the domain and never by a provider, coaching prose is guarded before it is
persisted, and the posting text is fenced as untrusted before it reaches a model.
"""
