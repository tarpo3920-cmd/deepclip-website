import os
import re
import sys
import uuid
import json
import hmac
import hashlib
import secrets
import sqlite3
import subprocess
import time
import base64
import shutil
import logging

from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deepclip")

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT.parent / "public"
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "app.db"

SECRET = os.getenv("SECRET_KEY", "unsafe-dev-secret-change-me").encode()
API_KEY = os.getenv("OPENAI_API_KEY", "")

TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini")
TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower().strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-admin-password")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_AUDIO_MB = int(os.getenv("MAX_AUDIO_MB", "24"))
STARTER_CREDITS = int(os.getenv("STARTER_CREDITS", "5"))
AUDIO_CHUNK_SECONDS = int(os.getenv("AUDIO_CHUNK_SECONDS", "480"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv(
    "GITHUB_REPOSITORY", "tarpo3920-cmd/deepclip-website"
)
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

KPAY_GITHUB_PATH = "storage/kpay"
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
MAX_URL_DOWNLOAD_SECONDS = 900

ALLOWED_TTS_VOICES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar"
}

app = FastAPI(title="DeepClip AI Video Recap")

if not PUBLIC.is_dir():
    raise RuntimeError(f"Public folder not found: {PUBLIC}")

app.mount("/public", StaticFiles(directory=str(PUBLIC)), name="public")


# ==================================================
# COMMON
# ==================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def validate_options(language, mode, ratio, fit_mode):
    if language not in ("my", "en", "th"):
        raise HTTPException(400, "Language မမှန်ပါ။")
    if mode not in ("recap", "pro", "subtitle"):
        raise HTTPException(400, "Mode မမှန်ပါ။")
    if ratio not in ("9:16", "3:4", "16:9", "1:1"):
        raise HTTPException(400, "Video ratio မမှန်ပါ။")
    if fit_mode not in ("crop", "blur"):
        raise HTTPException(400, "Fit mode မမှန်ပါ။")


# ==================================================
# AUTH AND DATABASE
# ==================================================

def pw_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 180000
    ).hex()
    return salt + "$" + digest


def pw_check(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        actual = pw_hash(password, salt).split("$", 1)[1]
        return hmac.compare_digest(actual, digest)
    except Exception:
        return False


def token_for(user):
    payload = json.dumps({
        "uid": user["id"],
        "exp": int(time.time()) + 60 * 60 * 24 * 14
    }, separators=(",", ":")).encode()

    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        SECRET, body.encode(), hashlib.sha256
    ).hexdigest()
    return body + "." + signature


def read_token(token):
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(
            SECRET, body.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return None

        raw = base64.urlsafe_b64decode(
            body + "=" * (-len(body) % 4)
        )
        payload = json.loads(raw)

        if payload["exp"] < time.time():
            return None

        return int(payload["uid"])
    except Exception:
        return None


def current_user(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "အကောင့်ဝင်ပါ။")

    uid = read_token(authorization[7:])
    if not uid:
        raise HTTPException(401, "Session သက်တမ်းကုန်သွားပါပြီ။")

    with db() as con:
        user = con.execute(
            """
            SELECT id,email,role,credits,is_premium
            FROM users WHERE id=?
            """,
            (uid,)
        ).fetchone()

    if not user:
        raise HTTPException(401, "အကောင့်မတွေ့ပါ။")
    return dict(user)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                credits INTEGER NOT NULL DEFAULT 0,
                is_premium INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cols = [
            row[1] for row in
            con.execute("PRAGMA table_info(users)").fetchall()
        ]
        if "is_premium" not in cols:
            con.execute("""
                ALTER TABLE users
                ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0
            """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                filename TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        existing = con.execute(
            "SELECT id FROM users WHERE email=?",
            (ADMIN_EMAIL,)
        ).fetchone()

        if not existing:
            cur = con.execute(
                """
                INSERT INTO users(email,password,role,credits,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    ADMIN_EMAIL, pw_hash(ADMIN_PASSWORD),
                    "admin", 999999, now_iso()
                )
            )
            con.execute(
                """
                INSERT INTO ledger(user_id,amount,reason,created_at)
                VALUES(?,?,?,?)
                """,
                (cur.lastrowid, 999999, "Admin initial balance", now_iso())
            )


init_db()


def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin သာ အသုံးပြုနိုင်ပါသည်။")


# ==================================================
# KPAY GITHUB STORAGE
# ==================================================

def github_headers():
    if not GITHUB_TOKEN:
        raise HTTPException(503, "GitHub Token မထည့်ရသေးပါ။")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


async def github_get_file(path):
    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{path}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            url, headers=github_headers(),
            params={"ref": GITHUB_BRANCH}
        )

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.error("GitHub GET: %s", response.text[:500])
        raise HTTPException(502, "GitHub Storage ဖတ်မရပါ။")
    return response.json()


async def github_save_file(path, content, message, sha=None):
    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/contents/{path}"
    )
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(
            url, headers=github_headers(), json=payload
        )

    if response.status_code not in (200, 201):
        logger.error("GitHub PUT: %s", response.text[:500])
        raise HTTPException(502, "GitHub Storage သိမ်းမရပါ။")
    return response.json()


async def read_kpay_metadata():
    item = await github_get_file(f"{KPAY_GITHUB_PATH}/current.json")
    if not item:
        return None
    try:
        return json.loads(base64.b64decode(item["content"]).decode("utf-8"))
    except Exception:
        raise HTTPException(502, "KPay QR Metadata ဖတ်မရပါ။")


async def get_kpay_image():
    metadata = await read_kpay_metadata()
    if not metadata:
        raise HTTPException(404, "KPay QR ပုံ မတင်ရသေးပါ။")

    filename = metadata.get("filename", "")
    media_type = metadata.get("media_type", "")

    if (
        not filename.startswith("kpay_qr_")
        or "/" in filename or "\\" in filename
        or media_type not in ("image/png", "image/jpeg", "image/webp")
    ):
        raise HTTPException(502, "KPay QR Data မမှန်ပါ။")

    image_url = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPOSITORY}/{GITHUB_BRANCH}/"
        f"{KPAY_GITHUB_PATH}/{filename}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(image_url)

    if response.status_code != 200:
        raise HTTPException(502, "KPay QR ပုံ ရယူမရပါ။")

    return Response(
        content=response.content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=300"}
    )


@app.post("/api/admin/payment-qr")
async def upload_payment_qr(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    admin = current_user(authorization)
    require_admin(admin)

    allowed = {
        "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
        "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
        "image/webp": (b"RIFF", ".webp")
    }
    content_type = (file.content_type or "").lower()

    try:
        if content_type not in allowed:
            raise HTTPException(415, "PNG, JPG, WEBP ကိုသာ တင်ပါ။")

        content = await file.read(5 * 1024 * 1024 + 1)
        if not content or len(content) > 5 * 1024 * 1024:
            raise HTTPException(413, "ပုံအရွယ်အစား 5MB ထက် မကျော်ရပါ။")

        signature, extension = allowed[content_type]
        if not content.startswith(signature):
            raise HTTPException(415, "ပုံဖိုင်အမျိုးအစား မမှန်ပါ။")
        if content_type == "image/webp":
            if len(content) < 12 or content[8:12] != b"WEBP":
                raise HTTPException(415, "WEBP ပုံမမှန်ပါ။")

        filename = f"kpay_qr_{uuid.uuid4().hex}{extension}"
        await github_save_file(
            f"{KPAY_GITHUB_PATH}/{filename}",
            content,
            "Upload KPay QR image"
        )

        metadata_path = f"{KPAY_GITHUB_PATH}/current.json"
        old = await github_get_file(metadata_path)
        metadata = {
            "filename": filename,
            "media_type": content_type
        }
        await github_save_file(
            metadata_path,
            json.dumps(metadata).encode("utf-8"),
            "Update current KPay QR image",
            sha=old["sha"] if old else None
        )
        return {"ok": True}
    finally:
        await file.close()


@app.get("/api/admin/payment-qr")
async def get_admin_payment_qr(
    authorization: Optional[str] = Header(None)
):
    admin = current_user(authorization)
    require_admin(admin)
    return await get_kpay_image()


@app.get("/api/payment-qr")
async def get_public_payment_qr():
    return await get_kpay_image()


# ==================================================
# DAILY LIMIT AND JOBS
# ==================================================

def myanmar_today():
    return (
        datetime.now(timezone.utc) +
        timedelta(hours=6, minutes=30)
    ).date().isoformat()


def create_job_with_daily_limit(uid, job_id, filename):
    today = myanmar_today()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        user = con.execute(
            "SELECT is_premium FROM users WHERE id=?", (uid,)
        ).fetchone()

        if not user:
            raise HTTPException(401, "အကောင့်မတွေ့ပါ။")

        if not user["is_premium"]:
            used = con.execute(
                """
                SELECT COUNT(*) AS n FROM jobs
                WHERE user_id=?
                AND status IN ('processing','done')
                AND substr(created_at,1,10)=?
                """,
                (uid, today)
            ).fetchone()["n"]

            if used >= 3:
                raise HTTPException(
                    402,
                    "ဒီနေ့ Free အသုံးပြုခွင့် ၃ ကြိမ် ပြည့်ပါပြီ။"
                )

        con.execute(
            """
            INSERT INTO jobs(id,user_id,filename,status,created_at)
            VALUES(?,?,?,?,?)
            """,
            (job_id, uid, filename, "processing", now_iso())
        )
        return bool(user["is_premium"])


# ==================================================
# FFMPEG HELPERS
# ==================================================

def run_ffmpeg(args, timeout=3600):
    try:
        process = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
            capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise HTTPException(500, "Render မှာ FFmpeg မတွေ့ပါ။")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Video processing အချိန်ကုန်သွားပါတယ်။")

    if process.returncode != 0:
        logger.error("FFmpeg: %s", (process.stderr or "")[-3000:])
        raise HTTPException(422, "Media processing မအောင်မြင်ပါ။")
    return process


def probe_duration(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path)
            ],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return 0
        return max(0, float(result.stdout.strip()))
    except Exception:
        return 0


def srt_time(seconds):
    ms = int(max(0, float(seconds)) * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def make_srt(segments, text_key="text"):
    output = []
    for i, segment in enumerate(segments, 1):
        text = (segment.get(text_key) or "").strip()
        if text:
            output.append(
                f"{i}\n"
                f"{srt_time(segment.get('start', 0))} --> "
                f"{srt_time(segment.get('end', 0))}\n"
                f"{text}\n"
            )
    return "\n".join(output)


# ==================================================
# OPENAI API
# ==================================================

def openai_headers():
    if not API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY မထည့်ရသေးပါ။")
    return {"Authorization": f"Bearer {API_KEY}"}


async def transcribe(path):
    if not path.is_file():
        raise HTTPException(400, "အသံဖိုင် မတွေ့ပါ။")
    if path.stat().st_size > MAX_AUDIO_MB * 1024 * 1024:
        raise HTTPException(
            413,
            f"အသံဖိုင် {MAX_AUDIO_MB}MB ကျော်နေပါသည်။"
        )

    try:
        async with httpx.AsyncClient(timeout=900) as client:
            with path.open("rb") as audio_file:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers=openai_headers(),
                    data={
                        "model": TRANSCRIBE_MODEL,
                        "response_format": "verbose_json"
                    },
                    files={
                        "file": (path.name, audio_file, "audio/mpeg")
                    }
                )
    except httpx.TimeoutException:
        raise HTTPException(504, "အသံစာသားထုတ်ရန် အချိန်ကုန်သွားပါတယ်။")
    except httpx.RequestError:
        raise HTTPException(502, "OpenAI ကို ဆက်သွယ်မရပါ။")

    if response.status_code >= 400:
        logger.error("Transcription error: %s", response.text[:2000])
        raise HTTPException(
            502,
            "Transcription မအောင်မြင်ပါ။ API Key, Billing, Model စစ်ပါ။"
        )

    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise HTTPException(502, "Transcription Data ဖတ်မရပါ။")


async def transcribe_long_audio(audio, folder):
    """
    Audio ကြာချိန်များလျှင် ၈ မိနစ်စီခွဲပြီး Transcribe လုပ်သည်။
    Segment timestamp များကို မူရင်း Video အချိန်နှင့် ကိုက်ညီအောင် ပြန်ပေါင်းသည်။
    """
    duration = probe_duration(audio)
    if duration <= 0:
        return await transcribe(audio)

    if audio.stat().st_size <= MAX_AUDIO_MB * 1024 * 1024:
        return await transcribe(audio)

    all_segments = []
    all_text = []
    detected_language = "unknown"

    chunk_seconds = max(120, min(AUDIO_CHUNK_SECONDS, 600))
    total_chunks = int((duration + chunk_seconds - 1) // chunk_seconds)

    for index in range(total_chunks):
        start = index * chunk_seconds
        chunk_path = folder / f"audio_chunk_{index:04d}.mp3"

        run_ffmpeg([
            "-ss", str(start),
            "-i", str(audio),
            "-t", str(chunk_seconds),
            "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "48k",
            str(chunk_path)
        ], timeout=600)

        if not chunk_path.exists() or not chunk_path.stat().st_size:
            raise HTTPException(422, "Audio အပိုင်းခွဲ၍ မရပါ။")

        if chunk_path.stat().st_size > MAX_AUDIO_MB * 1024 * 1024:
            raise HTTPException(
                413,
                "Audio အပိုင်းတစ်ခုသည် သတ်မှတ်ထားသော အရွယ်အစားထက် ကြီးနေပါသည်။"
            )

        result = await transcribe(chunk_path)
        if index == 0:
            detected_language = result.get("language", "unknown")

        chunk_text = (result.get("text") or "").strip()
        if chunk_text:
            all_text.append(chunk_text)

        for segment in result.get("segments", []):
            item = dict(segment)
            item["start"] = float(item.get("start", 0)) + start
            item["end"] = float(item.get("end", 0)) + start
            all_segments.append(item)

        try:
            chunk_path.unlink()
        except OSError:
            pass

    return {
        "text": " ".join(all_text).strip(),
        "language": detected_language,
        "segments": all_segments
    }


async def ai_text(system, prompt):
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    **openai_headers(),
                    "Content-Type": "application/json"
                },
                json=payload
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "AI စာသားထုတ်ရန် အချိန်ကုန်သွားပါတယ်။")
    except httpx.RequestError:
        raise HTTPException(502, "AI Server ကို ဆက်သွယ်မရပါ။")

    if response.status_code >= 400:
        logger.error("Text API error: %s", response.text[:2000])
        raise HTTPException(
            502,
            "AI စာသားထုတ်မရပါ။ API Key, Model, Billing စစ်ပါ။"
        )

    try:
        return response.json()["choices"][0]["message"]["content"]
    except Exception:
        raise HTTPException(502, "AI မှ စာသားပြန်မရပါ။")


async def generate_speech(text, folder, voice=None):
    """
    AI Voice ကို MP3 ထုတ်ပေးသည်။
    စာသားရှည်လျှင် အပိုင်းခွဲထုတ်ပြီး FFmpeg ဖြင့် ပေါင်းသည်။
    """
    if not text.strip():
        raise HTTPException(400, "Voice ထုတ်ရန် စာသားမရှိပါ။")

    voice = voice or TTS_VOICE
    if voice not in ALLOWED_TTS_VOICES:
        voice = TTS_VOICE

    chunks = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= 3500:
            chunks.append(remaining)
            break

        cut = remaining.rfind("။", 0, 3500)
        if cut < 1000:
            cut = remaining.rfind(" ", 0, 3500)
        if cut < 1000:
            cut = 3500

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    audio_files = []

    for index, chunk in enumerate(chunks):
        payload = {
            "model": TTS_MODEL,
            "voice": voice,
            "input": chunk,
            "instructions": (
                "Speak naturally and clearly in Burmese language. "
                "Use a warm, understandable narration style. "
                "Pronounce Burmese words carefully."
            ),
            "response_format": "mp3"
        }

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        **openai_headers(),
                        "Content-Type": "application/json"
                    },
                    json=payload
                )
        except httpx.TimeoutException:
            raise HTTPException(504, "AI Voice ထုတ်ရန် အချိန်ကုန်သွားပါတယ်။")
        except httpx.RequestError:
            raise HTTPException(502, "AI Voice Server ကို ဆက်သွယ်မရပါ။")

        if response.status_code >= 400:
            logger.error("TTS error: %s", response.text[:1500])
            raise HTTPException(
                502,
                "AI Voice မထုတ်နိုင်ပါ။ TTS Model, API Key, Billing စစ်ပါ။"
            )

        chunk_file = folder / f"voice_chunk_{index:04d}.mp3"
        chunk_file.write_bytes(response.content)
        audio_files.append(chunk_file)

    if len(audio_files) == 1:
        output = folder / "voice.mp3"
        shutil.copy2(audio_files[0], output)
        return output

    list_file = folder / "voice_concat.txt"
    list_file.write_text(
        "".join(
            "file '" + p.name.replace("'", "") + "'\n"
            for p in audio_files
        ),
        encoding="utf-8"
    )

    output = folder / "voice.mp3"
    run_ffmpeg([
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(output)
    ], timeout=900)

    return output


def parse_json_array(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        raise ValueError("JSON array not found")
    result = json.loads(match.group(0))
    if not isinstance(result, list):
        raise ValueError("Not an array")
    return result


async def translate_segments_to_burmese(segments):
    if not segments:
        return []

    translated = []

    for offset in range(0, len(segments), 25):
        batch = segments[offset:offset + 25]
        items = [
            {
                "id": offset + i,
                "text": (item.get("text") or "").strip()
            }
            for i, item in enumerate(batch)
        ]

        raw = await ai_text(
            (
                "You are a professional subtitle translator. "
                "Translate every supplied subtitle into natural, clear Burmese. "
                "Keep names, numbers and meaning accurate. "
                "Do not summarize or omit any item. "
                "Return ONLY a valid JSON array. "
                "Each item must have id and translation fields."
            ),
            json.dumps(items, ensure_ascii=False)
        )

        try:
            rows = parse_json_array(raw)
            mapping = {}
            for row in rows:
                if isinstance(row, dict) and "id" in row:
                    mapping[int(row["id"])] = str(
                        row.get("translation", "")
                    ).strip()

            for i, original in enumerate(batch):
                global_id = offset + i
                text = mapping.get(global_id, "")
                if not text:
                    text = (original.get("text") or "").strip()

                translated.append({
                    "start": original.get("start", 0),
                    "end": original.get("end", 0),
                    "text": text
                })

        except Exception:
            logger.exception("Subtitle translation batch failed")
            for original in batch:
                translated.append({
                    "start": original.get("start", 0),
                    "end": original.get("end", 0),
                    "text": (original.get("text") or "").strip()
                })

    return translated


# ==================================================
# VIDEO FORMAT, SUBTITLES AND HIGHLIGHTS
# ==================================================

def dimensions(ratio):
    return {
        "9:16": (720, 1280),
        "3:4": (900, 1200),
        "16:9": (1280, 720),
        "1:1": (1080, 1080)
    }.get(ratio, (720, 1280))


def format_filter(ratio, fit):
    width, height = dimensions(ratio)

    if fit == "blur":
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=24:12[blur];"
            f"[fg]scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease[front];"
            f"[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )

    return (
        f"[0:v]scale={width}:{height}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[v]"
    )


def subtitle_filter_path(path):
    value = str(path.resolve())
    value = value.replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    return value


def burn_subtitles_to_video(source, subtitle_file, output, ratio, fit):
    """
    Subtitle ကို Video ပေါ်မှာ Burn လုပ်သည်။
    Render FFmpeg တွင် subtitles/libass မပါလျှင် error ကို log လုပ်ပြီး
    Caller က subtitle မပါသော output ကို fallback လုပ်နိုင်သည်။
    """
    width, height = dimensions(ratio)
    video_filter = format_filter(ratio, fit)
    subtitle_path = subtitle_filter_path(subtitle_file)

    style = (
        "FontName=Noto Sans Myanmar,"
        "FontSize=20,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=45"
    )

    graph = (
        video_filter.replace("[v]", "[formatted]") +
        f";[formatted]subtitles='{subtitle_path}':"
        f"force_style='{style}',scale={width}:{height}[v]"
    )

    run_ffmpeg([
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output)
    ], timeout=2400)


async def make_highlights(transcript, segments, folder, ratio, fit):
    if not segments:
        return None, "Timestamp segment မရပါ။"

    compact = [
        {
            "start": s.get("start", 0),
            "end": s.get("end", 0),
            "text": (s.get("text") or "")[:240]
        }
        for s in segments[:500]
    ]

    raw = await ai_text(
        (
            "Select 3 to 6 useful video highlight moments. "
            "Each should be 8 to 35 seconds. Use supplied timestamps only. "
            "Return ONLY JSON: "
            "{\"clips\":[{\"start\":number,\"end\":number}]}"
        ),
        json.dumps(compact, ensure_ascii=False)[:60000]
    )

    try:
        match = re.search(r"\{.*\}", raw, re.S)
        clips = json.loads(match.group(0))["clips"] if match else []
    except Exception:
        clips = []

    valid = []
    duration = probe_duration(
        next(iter(folder.glob("source_video.*")), folder / "missing")
    )

    for clip in clips:
        try:
            start = max(0, float(clip["start"]))
            end = float(clip["end"])
            if duration:
                end = min(end, duration)
            if 6 <= end - start <= 45:
                valid.append((start, end))
        except Exception:
            pass

    source_files = [
        p for p in folder.glob("source_video.*")
        if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    ]

    if not source_files or not valid:
        return None, "Highlight ဖန်တီးရန် အချက်အလက်မလုံလောက်ပါ။"

    pieces = []
    for i, (start, end) in enumerate(valid[:6]):
        piece = folder / f"clip_{i}.mp4"
        run_ffmpeg([
            "-ss", str(start), "-i", str(source_files[0]),
            "-t", str(end - start),
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "aac",
            str(piece)
        ], timeout=600)
        if piece.exists() and piece.stat().st_size:
            pieces.append(piece)

    if not pieces:
        return None, "Highlight clip မရပါ။"

    listfile = folder / "concat.txt"
    listfile.write_text(
        "".join(
            "file '" + p.as_posix().replace("'", "'\\''") + "'\n"
            for p in pieces
        ),
        encoding="utf-8"
    )

    joined = folder / "joined.mp4"
    run_ffmpeg([
        "-f", "concat", "-safe", "0",
        "-i", str(listfile),
        "-c", "copy", str(joined)
    ], timeout=1200)

    output = folder / "highlights.mp4"
    run_ffmpeg([
        "-i", str(joined),
        "-filter_complex", format_filter(ratio, fit),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-c:a", "aac",
        "-movflags", "+faststart", str(output)
    ], timeout=1800)

    return output, None


# ==================================================
# URL DOWNLOAD
# ==================================================

def validate_video_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except Exception:
        raise HTTPException(400, "Video URL မမှန်ပါ။")

    if parsed.scheme != "https":
        raise HTTPException(400, "HTTPS URL ကိုသာ အသုံးပြုပါ။")
    if parsed.username or parsed.password:
        raise HTTPException(400, "ဒီ URL ကို အသုံးပြု၍မရပါ။")

    try:
        if parsed.port not in (None, 443):
            raise HTTPException(400, "URL Port မမှန်ပါ။")
    except ValueError:
        raise HTTPException(400, "URL Port မမှန်ပါ။")

    allowed = ("youtube.com", "youtu.be", "tiktok.com")
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(400, "YouTube သို့မဟုတ် TikTok URL ကိုသာ ထည့်ပါ။")
    if not parsed.path or parsed.path == "/":
        raise HTTPException(400, "Video link မမှန်ပါ။")

    return url.strip()


def download_public_video(url, folder):
    output_template = str(folder / "source_video.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings",
        "--socket-timeout", "30", "--retries", "2",
        "--max-filesize", f"{MAX_UPLOAD_MB}M",
        "-f", "best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template, url
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=MAX_URL_DOWNLOAD_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "URL Download အချိန်ကုန်သွားပါတယ်။")
    except FileNotFoundError:
        raise HTTPException(500, "yt-dlp မတွေ့ပါ။")

    if result.returncode != 0:
        logger.error("yt-dlp: %s", (result.stderr or "")[-3000:])
        raise HTTPException(
            422,
            "ဒီ Video link ကို ရယူမရပါ။ Public ဖြစ်ပြီး အသုံးပြုခွင့်ရှိသော Video ကိုသာ သုံးပါ။"
        )

    files = [
        p for p in folder.glob("source_video.*")
        if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    ]
    if not files:
        raise HTTPException(422, "Video ဖိုင် မရရှိပါ။")

    source = files[0]
    if source.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, "Video ဖိုင်အရွယ်အစား ကျော်နေပါသည်။")
    return source


# ==================================================
# SHARED VIDEO PROCESSOR
# ==================================================

async def process_video(
    source, original_filename, user,
    language, mode, ratio, fit_mode, job, folder,
    voice_enabled=False, voice_name=None, burn_subtitles=False
):
    audio = folder / "audio.mp3"
    output = folder / "formatted.mp4"
    subtitle_output = folder / "formatted_subtitled.mp4"
    source_srt = folder / "source_subtitles.srt"
    burmese_srt = folder / "subtitles.srt"
    recapfile = folder / "recap.txt"

    validate_options(language, mode, ratio, fit_mode)
    create_job_with_daily_limit(user["id"], job, original_filename)

    try:
        if not source.is_file() or source.stat().st_size == 0:
            raise HTTPException(400, "Video ဖိုင်အလွတ် ဖြစ်နေပါသည်။")

        if source.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, "Video ဖိုင်အရွယ်အစား ကျော်နေပါသည်။")

        run_ffmpeg([
            "-i", str(source), "-vn", "-ac", "1",
            "-ar", "16000", "-b:a", "64k", str(audio)
        ], timeout=1800)

        if not audio.exists() or not audio.stat().st_size:
            raise HTTPException(422, "Video မှ အသံထုတ်မရပါ။")

        transcript_data = await transcribe_long_audio(audio, folder)
        transcript = (transcript_data.get("text") or "").strip()

        if not transcript:
            raise HTTPException(422, "အသံမှ စာသားမရပါ။")

        detected_language = transcript_data.get("language", "unknown")
        segments = transcript_data.get("segments", [])

        if not segments:
            raise HTTPException(
                422,
                "AI မှ စာတန်းထိုး Timestamp မရရှိပါ။ ပြန်စမ်းပါ။"
            )

        source_srt.write_text(
            make_srt(segments),
            encoding="utf-8"
        )

        burmese_segments = await translate_segments_to_burmese(segments)
        burmese_srt.write_text(
            make_srt(burmese_segments),
            encoding="utf-8"
        )

        target = {
            "my": "Burmese",
            "en": "English",
            "th": "Thai"
        }[language]

        if mode == "pro":
            detail = (
                "Provide a detailed structured recap with headings "
                "and key points."
            )
        elif mode == "subtitle":
            detail = (
                "Provide a brief description of the video topic "
                "and key context."
            )
        else:
            detail = "Provide a concise recap with key bullet points."

        recap_text = await ai_text(
            (
                f"You summarize video transcripts in {target}. "
                "Be faithful to the transcript. Do not invent facts. "
                + detail
            ),
            "Summarize this transcript:\n\n" + transcript[:90000]
        )

        recapfile.write_text(recap_text, encoding="utf-8")

        # Video ratio conversion
        run_ffmpeg([
            "-i", str(source),
            "-filter_complex", format_filter(ratio, fit_mode),
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(output)
        ], timeout=2400)

        if not output.exists() or not output.stat().st_size:
            raise HTTPException(500, "Output Video ဖန်တီးမရပါ။")

        result = {
            "job_id": job,
            "recap": recap_text,
            "transcript": transcript[:10000],
            "detected_language": detected_language,
            "subtitle_language": "my",
            "subtitles_burned_in": False,
            "voice_generated": False,
            "downloads": {
                "video": f"/api/download/{job}/video",
                "srt": f"/api/download/{job}/srt",
                "source_srt": f"/api/download/{job}/source_srt",
                "text": f"/api/download/{job}/text"
            }
        }

        # Optional hardcoded Burmese subtitles
        if burn_subtitles:
            try:
                burn_subtitles_to_video(
                    source, burmese_srt, subtitle_output, ratio, fit_mode
                )
                if subtitle_output.exists() and subtitle_output.stat().st_size:
                    shutil.copy2(subtitle_output, output)
                    result["subtitles_burned_in"] = True
            except Exception as exc:
                logger.warning(
                    "Subtitle burn-in unavailable for job %s: %s",
                    job, str(exc)
                )
                result["subtitle_note"] = (
                    "Video ထဲ Subtitle ထည့်မရပါ။ SRT ဖိုင်ကို Download လုပ်နိုင်ပါသည်။"
                )

        # Optional Burmese AI narration
        if voice_enabled:
            voice_file = await generate_speech(
                recap_text, folder, voice_name
            )
            if not voice_file.exists() or not voice_file.stat().st_size:
                raise HTTPException(500, "AI Voice ဖိုင် မရရှိပါ။")

            narrated = folder / "narrated.mp4"
            run_ffmpeg([
                "-i", str(output),
                "-i", str(voice_file),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                str(narrated)
            ], timeout=1800)

            if narrated.exists() and narrated.stat().st_size:
                shutil.copy2(narrated, output)
                result["voice_generated"] = True
                result["downloads"]["voice"] = (
                    f"/api/download/{job}/voice"
                )

        if mode == "pro":
            highlight, error = await make_highlights(
                transcript, segments, folder, ratio, fit_mode
            )
            if highlight:
                result["downloads"]["highlights"] = (
                    f"/api/download/{job}/highlights"
                )
            else:
                result["highlight_note"] = error

        with db() as con:
            con.execute(
                "UPDATE jobs SET status='done' WHERE id=?", (job,)
            )

        return result

    except Exception:
        logger.exception("Video processing failed job=%s", job)
        with db() as con:
            con.execute(
                "UPDATE jobs SET status='failed' WHERE id=?", (job,)
            )
        raise


# ==================================================
# WEBSITE AND PWA
# ==================================================

@app.get("/icon-192.png")
async def icon_192():
    path = PUBLIC / "icon-192.png"
    if not path.is_file():
        raise HTTPException(404, "Icon မတွေ့ပါ။")
    return FileResponse(path, media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    path = PUBLIC / "icon-512.png"
    if not path.is_file():
        raise HTTPException(404, "Icon မတွေ့ပါ။")
    return FileResponse(path, media_type="image/png")


@app.get("/manifest.json")
async def pwa_manifest():
    path = PUBLIC / "manifest.json"
    if not path.is_file():
        raise HTTPException(404, "Manifest မတွေ့ပါ။")
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_service_worker():
    path = PUBLIC / "sw.js"
    if not path.is_file():
        raise HTTPException(404, "Service worker မတွေ့ပါ။")
    return FileResponse(
        path, media_type="application/javascript",
        headers={"Cache-Control": "no-cache"}
    )


@app.get("/", response_class=HTMLResponse)
async def home():
    path = PUBLIC / "index.html"
    if not path.is_file():
        raise HTTPException(404, "Website မတွေ့ပါ။")
    return path.read_text(encoding="utf-8")


@app.get("/sitemap.xml")
async def sitemap():
    path = PUBLIC / "sitemap.xml"
    if not path.is_file():
        raise HTTPException(404, "Sitemap မတွေ့ပါ။")
    return FileResponse(path, media_type="application/xml")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "ai_configured": bool(API_KEY),
        "transcribe_model": TRANSCRIBE_MODEL,
        "text_model": TEXT_MODEL,
        "tts_model": TTS_MODEL,
        "tts_configured": bool(API_KEY)
    }


# ==================================================
# REGISTER / LOGIN
# ==================================================

@app.post("/api/register")
async def register(
    email: str = Form(...),
    password: str = Form(...)
):
    email = email.strip().lower()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Email မှန်ကန်စွာ ထည့်ပါ။")
    if len(password) < 8:
        raise HTTPException(400, "Password အနည်းဆုံး ၈ လုံးထားပါ။")
    if email == ADMIN_EMAIL:
        raise HTTPException(409, "ဒီ Email ကို အသုံးပြု၍မရပါ။")

    with db() as con:
        try:
            cur = con.execute(
                """
                INSERT INTO users(email,password,role,credits,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    email, pw_hash(password), "user",
                    STARTER_CREDITS, now_iso()
                )
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "ဒီ Email ဖြင့် အကောင့်ရှိပြီးသားပါ။")

        user = con.execute(
            """
            SELECT id,email,role,credits,is_premium
            FROM users WHERE id=?
            """,
            (cur.lastrowid,)
        ).fetchone()

    return {"token": token_for(user), "user": dict(user)}


@app.post("/api/login")
async def login(
    email: str = Form(...),
    password: str = Form(...)
):
    with db() as con:
        user = con.execute(
            "SELECT * FROM users WHERE email=?",
            (email.strip().lower(),)
        ).fetchone()

    if not user or not pw_check(password, user["password"]):
        raise HTTPException(401, "Email သို့မဟုတ် Password မှားနေပါသည်။")

    return {
        "token": token_for(user),
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user["role"],
            "credits": user["credits"],
            "is_premium": user["is_premium"]
        }
    }


@app.get("/api/me")
async def me(authorization: Optional[str] = Header(None)):
    return {"user": current_user(authorization)}


# ==================================================
# VIDEO UPLOAD
# ==================================================

@app.post("/api/recap")
async def recap(
    file: UploadFile = File(...),
    language: str = Form("my"),
    mode: str = Form("recap"),
    ratio: str = Form("9:16"),
    fit_mode: str = Form("crop"),
    voice_enabled: bool = Form(False),
    voice_name: str = Form(TTS_VOICE),
    burn_subtitles: bool = Form(False),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(415, "MP4, MOV, MKV, WEBM ကိုသာ တင်ပါ။")

    validate_options(language, mode, ratio, fit_mode)

    job = uuid.uuid4().hex
    folder = DATA / job
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / ("source_video" + suffix)
    size = 0

    try:
        with source.open("wb") as output_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(
                        413,
                        f"Video {MAX_UPLOAD_MB}MB ထက် မကျော်ရပါ။"
                    )
                output_file.write(chunk)

        if size == 0:
            raise HTTPException(400, "Video ဖိုင်အလွတ် ဖြစ်နေပါသည်။")

        return await process_video(
            source, filename or "uploaded-video", user,
            language, mode, ratio, fit_mode, job, folder,
            voice_enabled, voice_name, burn_subtitles
        )

    except Exception:
        with db() as con:
            exists = con.execute(
                "SELECT id FROM jobs WHERE id=?", (job,)
            ).fetchone()

        if not exists:
            shutil.rmtree(folder, ignore_errors=True)
        raise

    finally:
        await file.close()


# ==================================================
# YOUTUBE / TIKTOK URL
# ==================================================

@app.post("/api/recap-url")
async def recap_url(
    url: str = Form(...),
    language: str = Form("my"),
    mode: str = Form("recap"),
    ratio: str = Form("9:16"),
    fit_mode: str = Form("crop"),
    voice_enabled: bool = Form(False),
    voice_name: str = Form(TTS_VOICE),
    burn_subtitles: bool = Form(False),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)
    validate_options(language, mode, ratio, fit_mode)
    safe_url = validate_video_url(url)

    job = uuid.uuid4().hex
    folder = DATA / job
    folder.mkdir(parents=True, exist_ok=True)

    try:
        source = download_public_video(safe_url, folder)
        return await process_video(
            source, "video-from-url" + source.suffix, user,
            language, mode, ratio, fit_mode, job, folder,
            voice_enabled, voice_name, burn_subtitles
        )
    except Exception:
        with db() as con:
            existing = con.execute(
                "SELECT id FROM jobs WHERE id=?", (job,)
            ).fetchone()
        if not existing:
            shutil.rmtree(folder, ignore_errors=True)
        raise


# ==================================================
# DOWNLOAD
# ==================================================

@app.get("/api/download/{job_id}/{kind}")
async def download(
    job_id: str,
    kind: str,
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Job ID မမှန်ပါ။")

    with db() as con:
        job = con.execute(
            "SELECT user_id,status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    if not job or (
        job["user_id"] != user["id"] and user["role"] != "admin"
    ):
        raise HTTPException(404, "ဖိုင်မတွေ့ပါ။")

    if job["status"] != "done":
        raise HTTPException(409, "Video processing မပြီးသေးပါ။")

    folder = DATA / job_id
    options = {
        "video": (folder / "formatted.mp4", "formatted-video.mp4"),
        "srt": (folder / "subtitles.srt", "burmese-subtitles.srt"),
        "source_srt": (
            folder / "source_subtitles.srt",
            "original-subtitles.srt"
        ),
        "text": (folder / "recap.txt", "video-recap.txt"),
        "highlights": (folder / "highlights.mp4", "ai-highlights.mp4"),
        "voice": (folder / "voice.mp3", "burmese-ai-voice.mp3")
    }

    if kind not in options:
        raise HTTPException(404, "Download အမျိုးအစား မတွေ့ပါ။")

    path, filename = options[kind]
    if not path.is_file():
        raise HTTPException(404, "ဖိုင်မတွေ့ပါ။")

    media_type = (
        "audio/mpeg" if kind == "voice"
        else "video/mp4" if kind in ("video", "highlights")
        else "text/plain; charset=utf-8"
    )

    return FileResponse(
        path,
        filename=filename,
        media_type=media_type
    )


# ==================================================
# ADMIN
# ==================================================

@app.get("/api/admin/overview")
async def admin_overview(
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)
    require_admin(user)

    with db() as con:
        users = con.execute(
            """
            SELECT id,email,role,credits,is_premium,created_at
            FROM users ORDER BY id DESC
            """
        ).fetchall()

        jobs = con.execute(
            """
            SELECT id,user_id,filename,status,created_at
            FROM jobs ORDER BY created_at DESC LIMIT 50
            """
        ).fetchall()

    return {
        "users": [dict(x) for x in users],
        "jobs": [dict(x) for x in jobs]
    }


@app.post("/api/admin/credits")
async def admin_credits(
    user_id: int = Form(...),
    amount: int = Form(...),
    authorization: Optional[str] = Header(None)
):
    admin = current_user(authorization)
    require_admin(admin)

    if amount == 0 or abs(amount) > 10000:
        raise HTTPException(
            400,
            "Credit ပမာဏ 1 မှ 10000 အတွင်း ဖြစ်ရပါမည်။"
        )

    with db() as con:
        target = con.execute(
            "SELECT id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "User မတွေ့ပါ။")

        con.execute(
            "UPDATE users SET credits=MAX(0,credits+?) WHERE id=?",
            (amount, user_id)
        )
        con.execute(
            """
            INSERT INTO ledger(user_id,amount,reason,created_at)
            VALUES(?,?,?,?)
            """,
            (user_id, amount, "Admin credit adjustment", now_iso())
        )

    return {"ok": True}


@app.post("/api/admin/premium")
async def admin_premium(
    user_id: int = Form(...),
    enabled: int = Form(...),
    authorization: Optional[str] = Header(None)
):
    admin = current_user(authorization)
    require_admin(admin)

    if enabled not in (0, 1):
        raise HTTPException(
            400,
            "enabled သည် 0 သို့မဟုတ် 1 ဖြစ်ရပါမည်။"
        )

    with db() as con:
        target = con.execute(
            "SELECT id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "User မတွေ့ပါ။")

        con.execute(
            "UPDATE users SET is_premium=? WHERE id=?",
            (enabled, user_id)
        )

    return {"ok": True, "is_premium": bool(enabled)}