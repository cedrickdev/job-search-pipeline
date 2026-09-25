"""Turning spoken audio into the transcript the simulator evaluates (§14-17).

A voice answer is not a new kind of answer — it is a text answer the candidate happened to
speak. So this module's whole job is to get from bytes to text and then get out of the way:
the transcript is what is persisted and graded, and the raw audio is transcribed and
immediately discarded (§17). Nothing downstream — the engine, the evaluator, the readiness
aggregation — ever sees the audio, only the `content` of an `InterviewAnswer`.

The seam is a `SpeechTranscriber` protocol with two implementations, exactly the
provider-neutral shape the LLM layer uses:

- `DeterministicTranscriber` — a fake that returns a canned transcript for a fixed set of
  audio blobs (and a deterministic fallback for the rest), so a test can drive a whole voice
  answer without a model or a binary. It is the transcription analogue of the deterministic
  document generator: it proves the contract without the environment.
- `WhisperCppTranscriber` — wraps V1's local `server.transcribe.transcribe` (whisper.cpp via
  ffmpeg), which already transcribes in a scratch directory it deletes. It runs the blocking
  subprocess off the event loop and maps V1's three failure types onto the domain's typed
  `InterviewErrorCode`s, so the service and the API speak one vocabulary of failure.

Audio never leaves this machine: transcription is a local subprocess, never a routed LLM
call, so the privacy question the LLM router answers does not even arise here.
"""
import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.app.domain.interview import InterviewErrorCode

# The audio bounds, re-stated from V1's transcriber so the API can reject an oversized or
# unsupported upload with a typed error *before* a byte is written to disk or a subprocess
# is spawned. Kept identical to `server.transcribe` so a clip V1 accepted is accepted here.
MAX_AUDIO_BYTES: int = 25 * 1024 * 1024
ALLOWED_AUDIO_TYPES: frozenset[str] = frozenset({
    "audio/webm", "audio/ogg", "audio/wav", "audio/x-wav",
    "audio/mpeg", "audio/mp4", "audio/m4a",
})


class TranscriptionError(Exception):
    """A voice answer could not be transcribed, as one of the domain's typed reasons.

    Carries an `InterviewErrorCode` — `TRANSCRIPTION_UNAVAILABLE` (no binary/model, or the
    subprocess failed), `AUDIO_TOO_LARGE`, or `UNSUPPORTED_AUDIO` — so the service re-raises
    it as an `InterviewError` and the API maps it to a status code without parsing prose. The
    detail is a short operator sentence and never carries the audio or a raw subprocess dump.
    """

    def __init__(self, code: InterviewErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class InterviewAudio:
    """One spoken answer's raw bytes, in flight to the transcriber and no further (§17).

    A transient value, not a domain model and never persisted: the audio exists only long
    enough to be transcribed, then it is dropped. `content_type` is the upload's declared MIME
    type, checked against `ALLOWED_AUDIO_TYPES` before any work is done.
    """

    content: bytes
    content_type: str


@dataclass(frozen=True)
class TranscriptResult:
    """The text a transcriber produced, and how sure it was.

    `text` is the transcript that becomes an answer's `content`. `confidence` is the
    transcriber's own confidence on the unit interval, or `None` when it cannot report one —
    whisper.cpp does not, so the whisper adapter always yields `None`, and a `VOICE` answer's
    `transcript_confidence` is nullable for exactly that reason.
    """

    text: str
    confidence: float | None = None


@runtime_checkable
class SpeechTranscriber(Protocol):
    """A replaceable speech-to-text engine, behind one shape (§16).

    One method, mirroring the `LLMProvider` seam: hand it audio, get back a transcript, or an
    `TranscriptionError` raised — never a sentinel a caller might read as an empty answer.
    """

    async def transcribe(self, audio: InterviewAudio) -> TranscriptResult: ...


def _reject_unusable(audio: InterviewAudio) -> None:
    """The two checks every transcriber runs first: supported type, bounded size."""
    if audio.content_type not in ALLOWED_AUDIO_TYPES:
        raise TranscriptionError(
            InterviewErrorCode.UNSUPPORTED_AUDIO,
            f"unsupported audio content type: {audio.content_type!r}")
    if len(audio.content) > MAX_AUDIO_BYTES:
        raise TranscriptionError(
            InterviewErrorCode.AUDIO_TOO_LARGE,
            f"audio exceeds the {MAX_AUDIO_BYTES}-byte limit")


class DeterministicTranscriber:
    """A transcriber with no environment — for tests and a text-first default (§16).

    Returns a canned transcript for any audio blob whose bytes are a registered key, and a
    deterministic, reproducible fallback (derived from the content hash) for the rest, so a
    test gets the same transcript every run without a model or a subprocess. It still runs the
    size and type gates, so a test can exercise `AUDIO_TOO_LARGE` and `UNSUPPORTED_AUDIO`
    without whisper installed. The default `confidence` is fixed and high; a canned entry may
    override it to rehearse a low-confidence transcript.
    """

    def __init__(self, *, transcripts: Mapping[bytes, str] | None = None,
                 confidence: float | None = 0.95) -> None:
        self._transcripts = dict(transcripts or {})
        self._confidence = confidence

    async def transcribe(self, audio: InterviewAudio) -> TranscriptResult:
        _reject_unusable(audio)
        canned = self._transcripts.get(audio.content)
        if canned is not None:
            return TranscriptResult(text=canned, confidence=self._confidence)
        digest = hashlib.sha256(audio.content).hexdigest()[:12]
        return TranscriptResult(
            text=f"[transcribed answer {digest}]", confidence=self._confidence)


# The V1 transcribe signature, injected so the adapter can be unit-tested without the real
# whisper.cpp binary: `(audio_bytes, content_type) -> transcript text`, raising V1's own
# `TranscriptionUnavailable` / `AudioTooLarge` / `UnsupportedAudio`.
WhisperTranscribeFn = Callable[[bytes, str | None], str]


class WhisperCppTranscriber:
    """The local whisper.cpp adapter — transcribe on this machine, then discard (§14-17).

    Wraps V1's `server.transcribe.transcribe`, which shells out to ffmpeg and whisper-cli in a
    `TemporaryDirectory` it deletes on exit, so the raw audio is gone the moment the call
    returns — the discard is V1's behaviour, kept, not re-implemented. The blocking subprocess
    runs in a worker thread so the event loop is never stalled. whisper.cpp reports no
    per-transcript confidence, so `confidence` is always `None`; the answer records that
    honestly rather than inventing a number.

    The transcribe function is injected (defaulting to V1's) purely so a test can substitute a
    stub and assert the exception mapping without the binary present.
    """

    def __init__(self, transcribe_fn: WhisperTranscribeFn | None = None) -> None:
        self._transcribe_fn = transcribe_fn or _default_whisper_transcribe

    async def transcribe(self, audio: InterviewAudio) -> TranscriptResult:
        _reject_unusable(audio)
        try:
            text = await asyncio.to_thread(
                self._transcribe_fn, audio.content, audio.content_type)
        except TranscriptionError:
            raise
        except Exception as exc:  # mapped to a typed domain error below
            raise _map_whisper_error(exc) from exc
        return TranscriptResult(text=text.strip(), confidence=None)


def _default_whisper_transcribe(audio_bytes: bytes, content_type: str | None) -> str:
    """Call V1's whisper.cpp transcriber with its default binary/model resolution.

    Imported lazily so importing this module never drags in the V1 `server`/`pipeline`
    packages (or their environment) unless a real whisper transcription is actually run.
    """
    from server.transcribe import transcribe as v1_transcribe
    return v1_transcribe(audio_bytes, content_type)


def _map_whisper_error(exc: Exception) -> TranscriptionError:
    """Map V1's transcriber exceptions onto the domain's typed `InterviewErrorCode`s.

    Matched by class name rather than by import, so this never has to import the V1 exception
    types (keeping the lazy-import boundary intact); anything unrecognised is the conservative
    `TRANSCRIPTION_UNAVAILABLE`, never surfaced as an untyped crash.
    """
    name = type(exc).__name__
    if name == "AudioTooLarge":
        return TranscriptionError(
            InterviewErrorCode.AUDIO_TOO_LARGE, "audio exceeds the transcriber's size limit")
    if name == "UnsupportedAudio":
        return TranscriptionError(
            InterviewErrorCode.UNSUPPORTED_AUDIO, "the transcriber rejected the audio format")
    return TranscriptionError(
        InterviewErrorCode.TRANSCRIPTION_UNAVAILABLE,
        "the speech transcriber is unavailable")
