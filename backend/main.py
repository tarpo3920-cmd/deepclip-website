
import os
import uuid
import shutil
import secrets
import hashlib
import asyncio
import ipaddress
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp

from fastapi import (
    FastAPI, UploadFile, File, Form, Header, HTTPException
)
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

# =====================================
# CONFIG
# =====================================

BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"
DATA.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = "https://api.openai.com/v1"

TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = "whisper-1"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

MAX_VIDEO_MB = 500
MAX_AUDIO_MB = 100

ALLOWED_TTS_VOICES = {
    "alloy", "echo", "fable", "onyx", "nova", "shimmer"
}

ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".mp4",
    ".mpeg", ".mpga", ".webm", ".ogg", ".flac"
}

app = FastAPI(title="DeepClip AI Video Platform")

USERS = {}
JOBS = {}

# =====================================
# AUTH
# =====================================

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def current_user(authorization):
    if not authorization:
        raise HTTPException(401, "Login required")

    token = authorization.replace("Bearer ", "").strip()

    for user in USERS.values():
        if secrets.compare_digest(user["token"], token):
            return user

    raise HTTPException(401, "Invalid session")


def require_api_key():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")


# =====================================
# OPENAI
# =====================================

async def ai_text(system, prompt):
    require_api_key()

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{OPENAI_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}"
            },
            json={
                "model": TEXT_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
        )

    if response.status_code != 200:
        raise RuntimeError("AI text request failed: " + response.text[:500])

    return response.json()["choices"][0]["message"]["content"]


async def transcribe(path):
    require_api_key()

    async with httpx.AsyncClient(timeout=600) as client:
        with path.open("rb") as audio:
            response = await client.post(
                f"{OPENAI_BASE}/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                data={
                    "model": TRANSCRIBE_MODEL,
                    "response_format": "verbose_json",
                },
                files={
                    "file": (
                        path.name,
                        audio,
                        "application/octet-stream",
                    )
                },
            )

    if response.status_code != 200:
        raise RuntimeError("Transcription failed: " + response.text[:500])

    return response.json()


async def translate_text(text, language="my"):
    languages = {
        "my": "Burmese",
        "en": "English",
        "th": "Thai",
    }

    target = languages.get(language)

    if not target:
        raise RuntimeError("Unsupported language")

    return await ai_text(
        f"Translate accurately into {target}. Return only the translation.",
        text,
    )


async def generate_speech(text, output, voice=None):
    require_api_key()

    voice = voice or TTS_VOICE

    if voice not in ALLOWED_TTS_VOICES:
        raise RuntimeError("Unsupported TTS voice")

    async with httpx.AsyncClient(timeout=600) as client:
        response = await client.post(
            f"{OPENAI_BASE}/audio/speech",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}"
            },
            json={
                "model": TTS_MODEL,
                "voice": voice,
                "input": text[:10000],
                "response_format": "mp3",
            },
        )

    if response.status_code != 200:
        raise RuntimeError("Speech generation failed")

    output.write_bytes(response.content)
    return output


# =====================================
# VIDEO URL SECURITY
# =====================================

def validate_video_url(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("Only HTTP and HTTPS URLs are supported")

    if not parsed.hostname:
        raise RuntimeError("Invalid video URL")

    hostname = parsed.hostname.lower()

    if hostname in ("localhost", "localhost.localdomain"):
        raise RuntimeError("Local URLs are not allowed")

    try:
        addresses = socket.getaddrinfo(hostname, None)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])

            if not ip.is_global:
                raise RuntimeError("Private or local addresses are not allowed")
    except socket.gaierror:
        raise RuntimeError("Website address could not be resolved")

    return True


# =====================================
# VIDEO DOWNLOAD
# =====================================

def download_video(url, folder):
    validate_video_url(url)

    output_template = str(folder / "source.%(ext)s")

    options = {
        "outtmpl": output_template,
        "format": "best[height<=1080]/best",
        "noplaylist": True,
        "max_filesize": MAX_VIDEO_MB * 1024 * 1024,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])

    files = [
        p for p in folder.glob("source.*")
        if p.is_file() and p.suffix != ".part"
    ]

    if not files:
        raise RuntimeError("Video download failed")

    return files[0]


# =====================================
# FFMPEG
# =====================================

def extract_audio(video, folder):
    audio = folder / "audio.mp3"

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=1800,
    )

    if not audio.exists():
        raise RuntimeError("Audio extraction failed")

    if audio.stat().st_size > MAX_AUDIO_MB * 1024 * 1024:
        raise RuntimeError("Audio file exceeds the allowed size")

    return audio


def make_srt(segments, output):
    def timestamp(seconds):
        milliseconds = int(float(seconds or 0) * 1000)

        hours = milliseconds // 3600000
        minutes = (milliseconds % 3600000) // 60000
        seconds_part = (milliseconds % 60000) // 1000
        ms = milliseconds % 1000

        return (
            f"{hours:02}:{minutes:02}:"
            f"{seconds_part:02},{ms:03}"
        )

    with output.open("w", encoding="utf-8") as file:
        for index, segment in enumerate(segments, 1):
            file.write(f"{index}\n")
            file.write(
                f"{timestamp(segment.get('start'))} --> "
                f"{timestamp(segment.get('end'))}\n"
            )
            file.write(segment.get("text", "").strip() + "\n\n")

    return output


# =====================================
# AUTOMATIC VIDEO PROCESSING
# =====================================

async def process_video_job(job_id, video, folder):
    job = JOBS[job_id]

    try:
        job["status"] = "processing"
        job["progress"] = 10
        job["message"] = "Extracting audio"

        audio = await asyncio.to_thread(
            extract_audio, video, folder
        )

        job["progress"] = 30
        job["message"] = "Transcribing video"

        transcript = await transcribe(audio)
        transcript_text = transcript.get("text", "")
        segments = transcript.get("segments", [])

        if not transcript_text.strip():
            raise RuntimeError("No speech was detected in this video")

        job["progress"] = 50
        job["message"] = "Generating AI recap"

        recap = await ai_text(
            "You are an expert video summarizer. "
            "Write a clear summary with key points, "
            "important details and a short conclusion.",
            transcript_text,
        )

        job["progress"] = 65
        job["message"] = "Translating into Burmese"

        burmese = await translate_text(transcript_text, "my")
        burmese_recap = await ai_text(
            "Write a natural, easy-to-understand Burmese summary "
            "of this video. Use clear headings and key points.",
            recap,
        )

        job["progress"] = 80
        job["message"] = "Creating subtitles"

        subtitle_file = folder / "subtitles.srt"
        make_srt(segments, subtitle_file)

        recap_file = folder / "recap.txt"
        recap_file.write_text(
            "AI VIDEO RECAP\n\n"
            + recap
            + "\n\nBURMESE RECAP\n\n"
            + burmese_recap
            + "\n\nBURMESE TRANSLATION\n\n"
            + burmese,
            encoding="utf-8",
        )

        job["progress"] = 90
        job["message"] = "Finishing"

        job["result"] = {
            "transcript": transcript_text,
            "recap": recap,
            "burmese_recap": burmese_recap,
            "translation": burmese,
            "subtitle_url": f"/api/jobs/{job_id}/download/subtitles",
            "recap_url": f"/api/jobs/{job_id}/download/recap",
        }

        job["status"] = "completed"
        job["progress"] = 100
        job["message"] = "Video processing completed"

    except Exception as error:
        job["status"] = "failed"
        job["message"] = str(error)
        job["progress"] = 0


# =====================================
# JOB CREATION
# =====================================

async def start_job(video, folder, user):
    job_id = uuid.uuid4().hex

    JOBS[job_id] = {
        "id": job_id,
        "user": user["email"],
        "status": "queued",
        "progress": 0,
        "message": "Waiting for processing",
        "result": None,
        "folder": str(folder),
    }

    asyncio.create_task(
        process_video_job(job_id, video, folder)
    )

    return {
        "success": True,
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/api/jobs/{job_id}",
    }


# =====================================
# BASIC ROUTES
# =====================================

@app.get("/")
async def home():
    return {
        "name": "DeepClip",
        "status": "online",
        "features": [
            "video-upload",
            "video-url",
            "automatic-transcription",
            "automatic-translation",
            "ai-recap",
            "subtitles",
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# =====================================
# REGISTER / LOGIN
# =====================================

@app.post("/api/register")
async def register(
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.lower().strip()

    if email in USERS:
        raise HTTPException(409, "Account already exists")

    if len(password) < 8:
        raise HTTPException(400, "Password too short")

    token = secrets.token_urlsafe(32)

    USERS[email] = {
        "email": email,
        "password": hash_password(password),
        "token": token,
    }

    return {
        "success": True,
        "token": token,
        "user": {"email": email},
    }


@app.post("/api/login")
async def login(
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.lower().strip()
    user = USERS.get(email)

    if not user or user["password"] != hash_password(password):
        raise HTTPException(401, "Incorrect email or password")

    token = secrets.token_urlsafe(32)
    user["token"] = token

    return {
        "success": True,
        "token": token,
        "user": {"email": email},
    }


@app.get("/api/me")
async def me(
    authorization: str | None = Header(default=None)
):
    user = current_user(authorization)
    return {"email": user["email"]}


# =====================================
# VIDEO UPLOAD
# =====================================

@app.post("/api/recap")
async def recap_upload(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    user = current_user(authorization)

    ext = Path(file.filename or "").suffix.lower()

    if ext not in {
        ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"
    }:
        raise HTTPException(400, "Unsupported video format")

    folder = DATA / f"job_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)

    video = folder / f"source{ext}"
    size = 0

    try:
        with video.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_VIDEO_MB * 1024 * 1024:
                    raise HTTPException(413, "Video too large")

                output.write(chunk)

        return await start_job(video, folder, user)

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


# =====================================
# VIDEO URL
# =====================================

@app.post("/api/recap-url")
async def recap_url(
    url: str = Form(...),
    authorization: str | None = Header(default=None),
):
    user = current_user(authorization)

    try:
        validate_video_url(url)
    except Exception as error:
        raise HTTPException(400, str(error))

    folder = DATA / f"job_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)

    try:
        video = await asyncio.to_thread(
            download_video, url, folder
        )

        return await start_job(video, folder, user)

    except Exception as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, str(error))


# =====================================
# JOB STATUS
# =====================================

@app.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    user = current_user(authorization)
    job = JOBS.get(job_id)

    if not job or job["user"] != user["email"]:
        raise HTTPException(404, "Job not found")

    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "result": job["result"],
    }


# =====================================
# DOWNLOAD RESULTS
# =====================================

@app.get("/api/jobs/{job_id}/download/{kind}")
async def download_result(
    job_id: str,
    kind: str,
    authorization: str | None = Header(default=None),
):
    user = current_user(authorization)
    job = JOBS.get(job_id)

    if not job or job["user"] != user["email"]:
        raise HTTPException(404, "Job not found")

    if job["status"] != "completed":
        raise HTTPException(409, "Processing is not completed")

    folder = Path(job["folder"])

    files = {
        "recap": folder / "recap.txt",
        "subtitles": folder / "subtitles.srt",
    }

    target = files.get(kind)

    if not target or not target.exists():
        raise HTTPException(404, "File not found")

    media_type = (
        "text/plain" if kind == "recap"
        else "application/x-subrip"
    )

    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
    )


# =====================================
# STANDALONE TRANSCRIPTION
# =====================================

@app.post("/api/transcribe")
async def api_transcribe(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    current_user(authorization)

    ext = Path(file.filename or "").suffix.lower()

    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(400, "Unsupported audio format")

    folder = DATA / f"transcribe_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)

    try:
        audio = folder / f"upload{ext}"
        size = 0

        with audio.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_AUDIO_MB * 1024 * 1024:
                    raise HTTPException(413, "Audio too large")

                output.write(chunk)

        result = await transcribe(audio)

        return {
            "success": True,
            "text": result.get("text", ""),
            "language": result.get("language"),
            "segments": result.get("segments", []),
        }

    finally:
        shutil.rmtree(folder, ignore_errors=True)


# =====================================
# TRANSLATION
# =====================================

@app.post("/api/translate")
async def api_translate(
    text: str = Form(...),
    target_language: str = Form("my"),
    authorization: str | None = Header(default=None),
):
    current_user(authorization)

    if not text.strip():
        raise HTTPException(400, "Text is required")

    translated = await translate_text(text, target_language)

    return {
        "success": True,
        "translation": translated,
        "translated_text": translated,
    }


# =====================================
# TEXT TO SPEECH
# =====================================

@app.post("/api/tts")
async def api_tts(
    text: str = Form(...),
    voice: str = Form(TTS_VOICE),
    authorization: str | None = Header(default=None),
):
    current_user(authorization)

    if not text.strip() or len(text) > 10000:
        raise HTTPException(400, "Invalid text length")

    folder = DATA / f"tts_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)

    try:
        output = folder / "speech.mp3"
        await generate_speech(text, output, voice)

        return FileResponse(
            output,
            media_type="audio/mpeg",
            filename="deepclip-speech.mp3",
            background=BackgroundTask(
                shutil.rmtree,
                folder,
                ignore_errors=True,
            ),
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
