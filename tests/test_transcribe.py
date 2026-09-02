# tests/test_transcribe.py
"""Voice transcription core: type/size guards, model presence (503), happy path with fakes."""
import pytest

from server import transcribe

# Fake ffmpeg: ignore args, just create the output wav path (last positional arg).
FAKE_FFMPEG = '''#!/usr/bin/env python3
import sys
out = sys.argv[-1]
open(out, "wb").write(b"RIFFfakewav")
'''

# Fake whisper-cli: write "<of>.txt" with canned text. Parses -of and -otxt.
FAKE_WHISPER = '''#!/usr/bin/env python3
import sys
of = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "-of":
        of = args[i + 1]
open(of + ".txt", "w").write("hello from whisper\\n")
'''

# Fake whisper-cli that leaks any ANTHROPIC_* env vars it received into the
# transcript, so the test can prove child_env() stripped them. Writes "clean"
# when none are present.
FAKE_WHISPER_ENV = '''#!/usr/bin/env python3
import os, sys
of = None
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "-of":
        of = args[i + 1]
leaked = sorted(k for k in os.environ if k.startswith("ANTHROPIC"))
open(of + ".txt", "w").write(" ".join(leaked) or "clean")
'''


def _install(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def test_unsupported_content_type_raises(tmp_path):
    with pytest.raises(transcribe.UnsupportedAudio):
        transcribe.transcribe(b"x", "text/plain",
                              model_path=tmp_path / "m.bin",
                              whisper_bin="x", ffmpeg_bin="x")


def test_audio_too_large_raises(tmp_path):
    big = b"0" * (transcribe.MAX_AUDIO_BYTES + 1)
    with pytest.raises(transcribe.AudioTooLarge):
        transcribe.transcribe(big, "audio/webm",
                              model_path=tmp_path / "m.bin",
                              whisper_bin="x", ffmpeg_bin="x")


def test_missing_model_raises_unavailable(tmp_path):
    with pytest.raises(transcribe.TranscriptionUnavailable):
        transcribe.transcribe(b"x", "audio/webm",
                              model_path=tmp_path / "nope.bin",
                              whisper_bin="x", ffmpeg_bin="x")


def test_happy_path_returns_text(tmp_path):
    model = tmp_path / "m.bin"
    model.write_bytes(b"modeldata")
    ff = _install(tmp_path, "ffmpeg", FAKE_FFMPEG)
    wh = _install(tmp_path, "whisper-cli", FAKE_WHISPER)
    text = transcribe.transcribe(b"audiobytes", "audio/webm",
                                 model_path=model, whisper_bin=wh, ffmpeg_bin=ff)
    assert text == "hello from whisper"


def test_file_sha256_matches(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    import hashlib
    assert transcribe.file_sha256(f) == hashlib.sha256(b"abc").hexdigest()


def test_verify_model_checksum_mismatch(tmp_path):
    model = tmp_path / "m.bin"
    model.write_bytes(b"data")
    with pytest.raises(transcribe.TranscriptionUnavailable):
        transcribe.verify_model(model, expected_sha="deadbeef")


def test_verify_model_reads_sha256_sidecar(tmp_path):
    import hashlib
    model = tmp_path / "m.bin"
    model.write_bytes(b"data")
    (tmp_path / "m.bin.sha256").write_text(hashlib.sha256(b"data").hexdigest())
    transcribe.verify_model(model)  # sidecar matches -> no raise


def test_verify_model_sidecar_mismatch_raises(tmp_path):
    model = tmp_path / "m.bin"
    model.write_bytes(b"data")
    (tmp_path / "m.bin.sha256").write_text("deadbeef")
    with pytest.raises(transcribe.TranscriptionUnavailable):
        transcribe.verify_model(model)


def test_transcribe_subprocess_env_strips_anthropic(tmp_path, monkeypatch):
    # Even though whisper.cpp never calls Anthropic, defence-in-depth: the child
    # process MUST NOT inherit credentials. Prove child_env() stripped all three.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://must-not-leak")
    model = tmp_path / "m.bin"
    model.write_bytes(b"modeldata")
    ff = _install(tmp_path, "ffmpeg", FAKE_FFMPEG)
    wh = _install(tmp_path, "whisper-cli", FAKE_WHISPER_ENV)
    text = transcribe.transcribe(b"audiobytes", "audio/webm",
                                 model_path=model, whisper_bin=wh, ffmpeg_bin=ff)
    assert text == "clean"
    assert "ANTHROPIC" not in text
