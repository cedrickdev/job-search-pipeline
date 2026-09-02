"""Voice transcription via whisper.cpp (spec §7).

Pipeline: bytes -> ffmpeg (16 kHz mono wav) -> whisper-cli -> text.
The binary/model are not installed by this project; absence -> TranscriptionUnavailable (503).
Injection: JOBSEARCH_WHISPER_BIN, JOBSEARCH_FFMPEG_BIN, paths.WHISPER_MODEL_PATH.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pipeline import paths
from server._env import child_env  # SECURITY: strip Anthropic creds from child procs

MAX_AUDIO_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {
    "audio/webm", "audio/ogg", "audio/wav", "audio/x-wav",
    "audio/mpeg", "audio/mp4", "audio/m4a",
}


class TranscriptionUnavailable(Exception):
    pass


class AudioTooLarge(Exception):
    pass


class UnsupportedAudio(Exception):
    pass


def resolve_whisper_bin() -> str:
    return os.environ.get("JOBSEARCH_WHISPER_BIN") or shutil.which("whisper-cli") or "whisper-cli"


def resolve_ffmpeg_bin() -> str:
    return os.environ.get("JOBSEARCH_FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_model(model_path=None, *, expected_sha: str | None = None) -> None:
    model_path = Path(model_path or paths.WHISPER_MODEL_PATH)
    if not model_path.is_file():
        raise TranscriptionUnavailable(f"whisper model not found: {model_path}")
    # Truth gate: the expected checksum is NEVER invented. It is either passed in
    # explicitly (tests) or read from a `<model>.sha256` sidecar that
    # scripts/fetch_whisper_model.sh computes from the downloaded bytes at install
    # time. When no sidecar exists, we verify presence only (no checksum).
    if expected_sha is None:
        sidecar = Path(str(model_path) + ".sha256")
        if sidecar.is_file():
            expected_sha = sidecar.read_text(encoding="utf-8").split()[0].strip()
    if expected_sha and file_sha256(model_path) != expected_sha:
        raise TranscriptionUnavailable("whisper model checksum mismatch")


def _bin_exists(name: str) -> bool:
    return shutil.which(name) is not None or Path(name).is_file()


def transcribe(audio_bytes: bytes, content_type: str | None, *,
               whisper_bin: str | None = None, ffmpeg_bin: str | None = None,
               model_path=None) -> str:
    if content_type not in ALLOWED_TYPES:
        raise UnsupportedAudio(str(content_type))
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise AudioTooLarge(len(audio_bytes))
    model_path = Path(model_path or paths.WHISPER_MODEL_PATH)
    verify_model(model_path)
    whisper_bin = whisper_bin or resolve_whisper_bin()
    ffmpeg_bin = ffmpeg_bin or resolve_ffmpeg_bin()
    if not _bin_exists(whisper_bin):
        raise TranscriptionUnavailable(f"whisper binary not found: {whisper_bin}")
    if not _bin_exists(ffmpeg_bin):
        raise TranscriptionUnavailable(f"ffmpeg not found: {ffmpeg_bin}")

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in"
        wav = Path(td) / "out.wav"
        out_base = Path(td) / "out"
        src.write_bytes(audio_bytes)
        try:
            subprocess.run([ffmpeg_bin, "-y", "-i", str(src), "-ar", "16000",
                            "-ac", "1", str(wav)], check=True, capture_output=True,
                           env=child_env())
            subprocess.run([whisper_bin, "-m", str(model_path), "-f", str(wav),
                            "-nt", "-otxt", "-of", str(out_base)],
                           check=True, capture_output=True, env=child_env())
        except subprocess.CalledProcessError as exc:
            raise TranscriptionUnavailable(f"transcription failed: {exc}") from exc
        txt = out_base.with_suffix(".txt")
        return txt.read_text(encoding="utf-8").strip() if txt.is_file() else ""
