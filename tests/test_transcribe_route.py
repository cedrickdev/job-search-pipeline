# tests/test_transcribe_route.py
"""POST /api/transcribe: 200 with fakes, 415 bad type, 503 missing model."""
from pipeline import paths


def _install(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


FAKE_FFMPEG = '''#!/usr/bin/env python3
import sys
open(sys.argv[-1], "wb").write(b"RIFFfakewav")
'''
FAKE_WHISPER = '''#!/usr/bin/env python3
import sys
args = sys.argv[1:]
of = args[args.index("-of") + 1]
open(of + ".txt", "w").write("transcribed text\\n")
'''


def test_transcribe_ok(api_client, tmp_path, monkeypatch):
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    monkeypatch.setattr(paths, "WHISPER_MODEL_PATH", model)
    monkeypatch.setenv("JOBSEARCH_FFMPEG_BIN", _install(tmp_path, "ffmpeg", FAKE_FFMPEG))
    monkeypatch.setenv("JOBSEARCH_WHISPER_BIN", _install(tmp_path, "whisper-cli", FAKE_WHISPER))
    r = api_client.post("/api/transcribe",
                        files={"file": ("clip.webm", b"audiobytes", "audio/webm")})
    assert r.status_code == 200
    assert r.json()["text"] == "transcribed text"


def test_transcribe_bad_type_415(api_client, tmp_path, monkeypatch):
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    monkeypatch.setattr(paths, "WHISPER_MODEL_PATH", model)
    r = api_client.post("/api/transcribe",
                        files={"file": ("note.txt", b"hello", "text/plain")})
    assert r.status_code == 415


def test_transcribe_missing_model_503(api_client, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WHISPER_MODEL_PATH", tmp_path / "nope.bin")
    r = api_client.post("/api/transcribe",
                        files={"file": ("clip.webm", b"audiobytes", "audio/webm")})
    assert r.status_code == 503
