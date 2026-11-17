import os
import uuid
import shutil
import secrets
import hashlib
from pathlib import Path

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

# =========================
# CONFIG
# =========================

BASE_DIR = Path(__file__).resolve().parent
DATA = BASE_DIR / "data"
DATA.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = "https://api.openai.com/v1"

TTS_MODEL = "gpt-4o-mini-tts"
TRANSCRIBE_MODEL = "whisper-1"
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")

TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

ALLOWED_TTS_VOICES = {
    "alloy", "echo", "fable", "onyx", "nova", "shimmer"
}

ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".mp4",
    ".mpeg", ".mpga", ".webm", ".ogg", ".flac"
}

MAX_AUDIO_MB = 100

app = FastAPI(title="DeepClip API")

# Demo users: in-memory only
USERS = {}


# =========================
# AUTH
# =========================

def hash_password(password: str):
    return hashlib.sha256(password.encode()).hexdigest()


def current_user(authorization: str | None):
    if not authorization:
        raise HTTPException(401, "Login required")

    token = authorization.replace("Bearer ", "").strip()

    for user in USERS.values():
        if secrets.compare_digest(user["token"], token):
            return user

    raise HTTPException(401, "Invalid session")


# =========================
# OPENAI HELPERS
# =========================

def require_api_key():
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY is not configured")


async def ai_text(system: str, prompt: str):
    require_api_key()

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{OPENAI_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
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
        raise HTTPException(502, "AI text request failed")

    return response.json()["choices"][0]["message"]["content"]


async def transcribe(path: Path):
    require_api_key()

    async with httpx.AsyncClient(timeout=300) as client:
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
        raise HTTPException(502, "Audio transcription failed")

    return response.json()


async def transcribe_long_audio(audio: Path, folder: Path):
    # Direct transcription for supported audio sizes.
    return await transcribe(audio)


async def generate_speech(text: str, folder: Path, voice=None):
    require_api_key()

    voice = voice or TTS_VOICE

    if voice not in ALLOWED_TTS_VOICES:
        raise HTTPException(400, "Unsupported voice")

    output = folder / f"{uuid.uuid4().hex}.mp3"

    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(
            f"{OPENAI_BASE}/audio/speech",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": TTS_MODEL,
                "voice": voice,
                "input": text,
                "response_format": "mp3",
            },
        )

    if response.status_code != 200:
        raise HTTPException(502, "Speech generation failed")

    output.write_bytes(response.content)
    return output


# =========================
# BASIC ROUTES
# =========================

@app.get("/")
async def home():
    return {
        "name": "DeepClip API",
        "status": "online",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================
# REGISTER / LOGIN
# =========================

@app.post("/api/register")
async def register(
    email: str = Form(...),
    password: str = Form(...),
):
    email = email.lower().strip()

    if email in USERS:
        raise HTTPException(409, "Account already exists")

    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

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
async def me(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    return {"email": user["email"]}


# =========================
# SPEECH TO TEXT
# =========================

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
        audio_path = folder / f"upload{ext}"
        size = 0

        with audio_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_AUDIO_MB * 1024 * 1024:
                    raise HTTPException(413, "Audio file too large")

                output.write(chunk)

        result = await transcribe_long_audio(audio_path, folder)

        return {
            "success": True,
            "text": result.get("text", ""),
            "language": result.get("language"),
            "segments": result.get("segments", []),
        }

    finally:
        shutil.rmtree(folder, ignore_errors=True)


# =========================
# TRANSLATION
# =========================

@app.post("/api/translate")
async def api_translate(
    text: str = Form(...),
    target_language: str = Form("my"),
    authorization: str | None = Header(default=None),
):
    current_user(authorization)

    if not text.strip():
        raise HTTPException(400, "Text is required")

    languages = {
        "my": "Burmese",
        "en": "English",
        "th": "Thai",
    }

    target = languages.get(target_language)

    if not target:
        raise HTTPException(400, "Unsupported target language")

    translated = await ai_text(
        f"You are a professional translator. Translate into {target}. "
        "Preserve the original meaning. Return only the translation.",
        text,
    )

    return {
        "success": True,
        "translation": translated,
        "translated_text": translated,
    }


# =========================
# TEXT TO SPEECH
# =========================

@app.post("/api/tts")
async def api_tts(
    text: str = Form(...),
    voice: str = Form(TTS_VOICE),
    authorization: str | None = Header(default=None),
):
    current_user(authorization)

    if not text.strip():
        raise HTTPException(400, "Text is required")

    if len(text) > 10000:
        raise HTTPException(400, "Text is too long")

    folder = DATA / f"tts_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)

    try:
        output = await generate_speech(text, folder, voice)

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