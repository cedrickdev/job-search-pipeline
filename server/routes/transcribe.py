"""Multipart transcription endpoint."""
import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile

from server import transcribe

router = APIRouter(prefix="/api")


@router.post("/transcribe")
async def post_transcribe(file: UploadFile = File(...)):
    data = await file.read()
    try:
        text = await asyncio.to_thread(transcribe.transcribe, data, file.content_type)
    except transcribe.UnsupportedAudio:
        raise HTTPException(415, "unsupported audio type")
    except transcribe.AudioTooLarge:
        raise HTTPException(413, "audio too large")
    except transcribe.TranscriptionUnavailable as exc:
        raise HTTPException(503, str(exc))
    return {"text": text}
