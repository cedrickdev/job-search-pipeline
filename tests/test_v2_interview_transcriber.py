# tests/test_v2_interview_transcriber.py
"""La transcription : des octets au texte, puis on s'efface (§14-17).

Une réponse vocale n'est qu'une réponse texte que le candidat a prononcée. Le transcripteur a
donc un seul rôle — produire le texte évalué ensuite — et deux implémentations derrière le
protocole `SpeechTranscriber` : le `DeterministicTranscriber` sans environnement, pour les
tests, et le `WhisperCppTranscriber` local. On vérifie ici les garde-fous communs (type et
taille rejetés *avant* tout travail) et la traduction des échecs de whisper.cpp vers les codes
typés du domaine — sans binaire ni modèle.
"""
import pytest

from backend.app.domain.interview import InterviewErrorCode
from backend.app.interview.transcriber import (
    ALLOWED_AUDIO_TYPES,
    MAX_AUDIO_BYTES,
    DeterministicTranscriber,
    InterviewAudio,
    TranscriptionError,
    WhisperCppTranscriber,
)

pytestmark = pytest.mark.asyncio


def _audio(content=b"opus-bytes", content_type="audio/webm"):
    return InterviewAudio(content=content, content_type=content_type)
async def test_deterministic_transcriber_returns_a_canned_transcript():
    """A registered blob transcribes to its canned text, at the configured confidence."""
    audio = _audio(content=b"a-known-clip")
    transcriber = DeterministicTranscriber(
        transcripts={b"a-known-clip": "Bonjour, voici ma réponse."}, confidence=0.5)
    result = await transcriber.transcribe(audio)
    assert result.text == "Bonjour, voici ma réponse."
    assert result.confidence == 0.5


async def test_deterministic_transcriber_falls_back_reproducibly():
    """An unregistered blob gets a stable, content-derived transcript — same every run."""
    transcriber = DeterministicTranscriber()
    first = await transcriber.transcribe(_audio(content=b"unregistered"))
    second = await transcriber.transcribe(_audio(content=b"unregistered"))
    assert first.text == second.text
    assert first.text.startswith("[transcribed answer ")
    assert first.confidence == 0.95


async def test_unsupported_media_type_is_rejected_before_any_work():
    """A content type outside the allow-list raises `UNSUPPORTED_AUDIO`."""
    transcriber = DeterministicTranscriber()
    assert "audio/flac" not in ALLOWED_AUDIO_TYPES
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(_audio(content_type="audio/flac"))
    assert caught.value.code is InterviewErrorCode.UNSUPPORTED_AUDIO


async def test_oversized_audio_is_rejected():
    """Audio past `MAX_AUDIO_BYTES` raises `AUDIO_TOO_LARGE`, whatever the type."""
    transcriber = DeterministicTranscriber()
    oversized = _audio(content=b"\x00" * (MAX_AUDIO_BYTES + 1))
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(oversized)
    assert caught.value.code is InterviewErrorCode.AUDIO_TOO_LARGE
class AudioTooLarge(Exception):
    """A stub with V1's exception class name, matched by `_map_whisper_error` by name."""


class UnsupportedAudio(Exception):
    """A stub with V1's exception class name."""


class WhisperExploded(Exception):
    """An unrecognised failure — mapped conservatively to `TRANSCRIPTION_UNAVAILABLE`."""


async def test_whisper_adapter_transcribes_and_strips_reporting_no_confidence():
    """A successful transcription returns stripped text and `None` confidence (whisper reports none)."""
    def fake_transcribe(audio_bytes, content_type):
        return "  la réponse transcrite  "
    transcriber = WhisperCppTranscriber(transcribe_fn=fake_transcribe)
    result = await transcriber.transcribe(_audio())
    assert result.text == "la réponse transcrite"
    assert result.confidence is None


@pytest.mark.parametrize("raised, expected", [
    (AudioTooLarge, InterviewErrorCode.AUDIO_TOO_LARGE),
    (UnsupportedAudio, InterviewErrorCode.UNSUPPORTED_AUDIO),
    (WhisperExploded, InterviewErrorCode.TRANSCRIPTION_UNAVAILABLE),
])
async def test_whisper_adapter_maps_failures_to_typed_codes(raised, expected):
    """V1's failures become the domain's typed `InterviewErrorCode`s; the unknown is conservative."""
    def failing(audio_bytes, content_type):
        raise raised("boom")
    transcriber = WhisperCppTranscriber(transcribe_fn=failing)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(_audio())
    assert caught.value.code is expected


async def test_whisper_adapter_rejects_bad_input_before_spawning_the_subprocess():
    """The size/type gates run first, so a dead upload never reaches the transcribe function."""
    called = False

    def fake_transcribe(audio_bytes, content_type):
        nonlocal called
        called = True
        return "unreached"

    transcriber = WhisperCppTranscriber(transcribe_fn=fake_transcribe)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber.transcribe(_audio(content_type="text/plain"))
    assert caught.value.code is InterviewErrorCode.UNSUPPORTED_AUDIO
    assert called is False


