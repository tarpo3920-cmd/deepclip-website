
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

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT.parent / "public"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "app.db"

SECRET = os.getenv("SECRET_KEY", "unsafe-dev-secret-change-me").encode()
API_KEY = os.getenv("OPENAI_API_KEY", "")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower().strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-this-admin-password")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
STARTER_CREDITS = int(os.getenv("STARTER_CREDITS", "5"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.getenv(
    "GITHUB_REPOSITORY", "tarpo3920-cmd/deepclip-website"
)
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
KPAY_GITHUB_PATH = "storage/kpay"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
MAX_URL_DOWNLOAD_SECONDS = 900

app = FastAPI(title="DeepClip Mobile MVP")
app.mount("/public", StaticFiles(directory=str(PUBLIC)), name="public")


# --------------------------------------------------
# Database and authentication
# --------------------------------------------------

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


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
    payload = json.dumps(
        {
            "uid": user["id"],
            "exp": int(time.time()) + 60 * 60 * 24 * 14
        },
        separators=(",", ":")
    ).encode()

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
        raise HTTPException(
            401, "Session သက်တမ်းကုန်သွားပါပြီ။ ပြန်ဝင်ပါ။"
        )

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
            r[1] for r in
            con.execute("PRAGMA table_info(users)").fetchall()
        ]

        if "is_premium" not in cols:
            con.execute(
                """
                ALTER TABLE users
                ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0
                """
            )

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
                INSERT INTO users(
                    email,password,role,credits,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    ADMIN_EMAIL,
                    pw_hash(ADMIN_PASSWORD),
                    "admin",
                    999999,
                    datetime.now(timezone.utc).isoformat()
                )
            )

            con.execute(
                """
                INSERT INTO ledger(user_id,amount,reason,created_at)
                VALUES(?,?,?,?)
                """,
                (
                    cur.lastrowid,
                    999999,
                    "Admin initial balance",
                    datetime.now(timezone.utc).isoformat()
                )
            )


init_db()


def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin သာ အသုံးပြုနိုင်ပါသည်။")


# --------------------------------------------------
# KPay QR - GitHub Storage
# --------------------------------------------------

def github_headers():
    if not GITHUB_TOKEN:
        raise HTTPException(
            503,
            "GitHub Token မထည့်ရသေးပါ။ Render Environment ကို စစ်ပါ။"
        )

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
            url,
            headers=github_headers(),
            params={"ref": GITHUB_BRANCH}
        )

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        raise HTTPException(
            502, "GitHub မှ Storage ဖိုင်ကို ဖတ်မရပါ။"
        )

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
            url,
            headers=github_headers(),
            json=payload
        )

    if response.status_code not in (200, 201):
        raise HTTPException(
            502,
            "GitHub Storage မှာ သိမ်းမရပါ။ Token Permission ကို စစ်ပါ။"
        )

    return response.json()


async def read_kpay_metadata():
    item = await github_get_file(
        f"{KPAY_GITHUB_PATH}/current.json"
    )

    if not item:
        return None

    try:
        raw = base64.b64decode(item["content"])
        return json.loads(raw.decode("utf-8"))
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
        or "/" in filename
        or "\\" in filename
        or media_type not in (
            "image/png", "image/jpeg", "image/webp"
        )
    ):
        raise HTTPException(502, "KPay QR Storage Data မမှန်ပါ။")

    image_url = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPOSITORY}/{GITHUB_BRANCH}/"
        f"{KPAY_GITHUB_PATH}/{filename}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(image_url)

    if response.status_code != 200:
        raise HTTPException(
            502, "GitHub မှ KPay QR ပုံ ရယူမရပါ။"
        )

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

    allowed_types = {
        "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
        "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
        "image/webp": (b"RIFF", ".webp")
    }

    content_type = (file.content_type or "").lower()

    if content_type not in allowed_types:
        raise HTTPException(
            415, "PNG, JPG သို့မဟုတ် WEBP ပုံကိုသာ တင်ပါ။"
        )

    try:
        content = await file.read(5 * 1024 * 1024 + 1)

        if not content or len(content) > 5 * 1024 * 1024:
            raise HTTPException(413, "ပုံအရွယ်အစား 5MB ထက် မကျော်ရပါ။")

        signature, extension = allowed_types[content_type]

        if not content.startswith(signature):
            raise HTTPException(415, "ပုံဖိုင်အမျိုးအစား မမှန်ပါ။")

        if content_type == "image/webp":
            if len(content) < 12 or content[8:12] != b"WEBP":
                raise HTTPException(415, "WEBP ပုံဖိုင် မမှန်ပါ။")

        filename = f"kpay_qr_{uuid.uuid4().hex}{extension}"
        image_path = f"{KPAY_GITHUB_PATH}/{filename}"

        await github_save_file(
            image_path, content, "Upload KPay QR image"
        )

        metadata_path = f"{KPAY_GITHUB_PATH}/current.json"
        old_metadata = await github_get_file(metadata_path)

        metadata = {
            "filename": filename,
            "media_type": content_type
        }

        await github_save_file(
            metadata_path,
            json.dumps(metadata).encode("utf-8"),
            "Update current KPay QR image",
            sha=old_metadata["sha"] if old_metadata else None
        )

        return {
            "ok": True,
            "message": "KPay QR ပုံ Upload အောင်မြင်ပါသည်။"
        }

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


# --------------------------------------------------
# Daily usage
# --------------------------------------------------

def myanmar_today():
    return (
        datetime.now(timezone.utc) +
        timedelta(hours=6, minutes=30)
    ).date().isoformat()


def check_and_record_daily_use(uid):
    today = myanmar_today()

    with db() as con:
        row = con.execute(
            "SELECT is_premium FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not row:
            raise HTTPException(401, "အကောင့်မတွေ့ပါ။")

        if not row["is_premium"]:
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
                    "ဒီနေ့ Free အသုံးပြုခွင့် ၃ ကြိမ် ပြည့်သွားပါပြီ။ ဆက်သုံးရန် Premium ဝယ်ယူပါ။"
                )

        return bool(row["is_premium"])


# --------------------------------------------------
# FFmpeg and video processing
# --------------------------------------------------

def run_ffmpeg(args, timeout=3600):
    try:
        process = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner",
                "-loglevel", "error"
            ] + args,
            capture_output=True,
            text=True,
            timeout=timeout
        )
    except FileNotFoundError:
        raise HTTPException(500, "FFmpeg မတွေ့ပါ။ FFmpeg install လုပ်ပါ။")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Video processing အချိန်ကုန်သွားပါတယ်။")

    if process.returncode:
        raise HTTPException(
            400, "Video ကို process မလုပ်နိုင်ပါ။ Format ကို စစ်ပါ။"
        )

    return process


def srt_time(seconds):
    ms = int(max(0, float(seconds)) * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def make_srt(segments):
    output = []

    for i, segment in enumerate(segments, 1):
        text = (segment.get("text") or "").strip()

        if text:
            output.append(
                f"{i}\n"
                f"{srt_time(segment.get('start', 0))} --> "
                f"{srt_time(segment.get('end', 0))}\n"
                f"{text}\n"
            )

    return "\n".join(output)


async def transcribe(path):
    if not API_KEY:
        raise HTTPException(
            503,
            "AI API key မထည့်ရသေးပါ။ Render Environment ကို စစ်ပါ။"
        )

    headers = {"Authorization": f"Bearer {API_KEY}"}

    async with httpx.AsyncClient(timeout=900) as client:
        with path.open("rb") as audio_file:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                data={
                    "model": TRANSCRIBE_MODEL,
                    "response_format": "verbose_json"
                },
                files={
                    "file": (path.name, audio_file, "audio/mpeg")
                }
            )

    if response.status_code >= 400:
        raise HTTPException(
            502,
            "AI transcription မအောင်မြင်ပါ။ API key, billing နှင့် ဖိုင်အရွယ်အစားကို စစ်ပါ။"
        )

    return response.json()


async def ai_text(system, prompt):
    if not API_KEY:
        raise HTTPException(503, "OpenAI API key မထည့်ရသေးပါ။")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.25
    }

    async with httpx.AsyncClient(timeout=240) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload
        )

    if response.status_code >= 400:
        raise HTTPException(
            502,
            "AI recap မအောင်မြင်ပါ။ Text model/API ခွင့်ပြုချက်ကို စစ်ပါ။"
        )

    return response.json()["choices"][0]["message"]["content"]


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
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=24:12[blur];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[front];"
            f"[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
        )

    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[v]"
    )


async def make_highlights(transcript, segments, folder, ratio, fit):
    if not segments:
        return None, "အသံမှ timestamp segment မရသောကြောင့် highlight clip မဖန်တီးနိုင်ပါ။"

    compact = []

    for segment in segments[:500]:
        compact.append({
            "start": segment.get("start", 0),
            "end": segment.get("end", 0),
            "text": (segment.get("text") or "")[:240]
        })

    raw = await ai_text(
        (
            "You select useful, self-contained highlight moments from a video "
            "transcript. Return ONLY valid JSON: "
            "{\"clips\":[{\"start\":number,\"end\":number,\"reason\":\"short reason\"}]}. "
            "Select 3 to 6 moments. Each should be 8 to 35 seconds, grounded "
            "in the supplied segments. Do not invent timestamps."
        ),
        "Transcript segments:\n" +
        json.dumps(compact, ensure_ascii=False)[:60000]
    )

    try:
        match = re.search(r"\{.*\}", raw, re.S)
        clips = json.loads(match.group(0))["clips"] if match else []
    except Exception:
        clips = []

    valid = []

    for clip in clips:
        try:
            start = max(0, float(clip["start"]))
            end = float(clip["end"])

            if 6 <= end - start <= 45:
                valid.append((start, end))
        except Exception:
            pass

    valid = valid[:6]

    if not valid:
        return None, "AI က အသုံးပြုနိုင်သော highlight timestamp မရွေးနိုင်ပါ။"

    source_files = [
        p for p in folder.glob("source_video.*")
        if p.is_file()
    ]

    if not source_files:
        return None, "မူရင်း Video ဖိုင် မတွေ့ပါ။"

    source = source_files[0]
    pieces = []

    for i, (start, end) in enumerate(valid):
        piece = folder / f"clip_{i}.mp4"

        run_ffmpeg(
            [
                "-ss", str(start),
                "-i", str(source),
                "-t", str(end - start),
                "-c", "copy",
                str(piece)
            ],
            timeout=600
        )

        if piece.exists() and piece.stat().st_size > 0:
            pieces.append(piece)

    if not pieces:
        return None, "Highlight clip များ ဖန်တီးမရပါ။"

    listfile = folder / "concat.txt"
    listfile.write_text(
        "".join(
            "file '" + p.as_posix().replace("'", "'\\''") + "'\n"
            for p in pieces
        ),
        encoding="utf-8"
    )

    joined = folder / "joined.mp4"

    run_ffmpeg(
        [
            "-f", "concat", "-safe", "0",
            "-i", str(listfile),
            "-c", "copy",
            str(joined)
        ],
        timeout=1200
    )

    output = folder / "highlights.mp4"

    run_ffmpeg(
        [
            "-i", str(joined),
            "-filter_complex", format_filter(ratio, fit),
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output)
        ],
        timeout=1800
    )

    return output, None


# --------------------------------------------------
# YouTube / TikTok URL processing
# --------------------------------------------------

def validate_video_url(url):
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except Exception:
        raise HTTPException(400, "Video URL မမှန်ပါ။")

    if parsed.scheme != "https":
        raise HTTPException(400, "HTTPS URL ကိုသာ အသုံးပြုပါ။")

    allowed_hosts = (
        "youtube.com",
        "youtu.be",
        "tiktok.com"
    )

    allowed = any(
        host == domain or host.endswith("." + domain)
        for domain in allowed_hosts
    )

    if not allowed:
        raise HTTPException(
            400,
            "YouTube သို့မဟုတ် TikTok URL ကိုသာ ထည့်ပါ။"
        )

    if not parsed.path or parsed.path == "/":
        raise HTTPException(400, "Video link မမှန်ပါ။")

    return url.strip()


def download_public_video(url, folder):
    """
    Public/authorized video URL only.
    Does not use cookies or bypass access restrictions.
    """

    output_template = str(folder / "source_video.%(ext)s")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "2",
        "--max-filesize", f"{MAX_UPLOAD_MB}M",
        "-f", "best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=MAX_URL_DOWNLOAD_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            504, "URL မှ Video ရယူရန် အချိန်ကုန်သွားပါပြီ။"
        )

    if result.returncode != 0:
        raise HTTPException(
            422,
            "ဒီ Video link ကို ရယူမရပါ။ Public ဖြစ်ကြောင်း၊ အသုံးပြုခွင့်ရှိကြောင်းနှင့် Platform က ခွင့်ပြုထားကြောင်း စစ်ပါ။"
        )

    files = [
        p for p in folder.glob("source_video.*")
        if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    ]

    if not files:
        raise HTTPException(
            422, "URL မှ အသုံးပြုနိုင်သော Video ဖိုင် မရရှိပါ။"
        )

    source = files[0]

    if source.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            413,
            f"Video ဖိုင်အရွယ်အစား {MAX_UPLOAD_MB}MB ထက် ကျော်နေပါသည်။"
        )

    return source


# --------------------------------------------------
# Shared recap processor for uploaded and URL videos
# --------------------------------------------------

async def process_video(
    source,
    original_filename,
    user,
    language,
    mode,
    ratio,
    fit_mode,
    job,
    folder
):
    audio = folder / "audio.mp3"
    output = folder / "formatted.mp4"
    srt = folder / "subtitles.srt"
    recapfile = folder / "recap.txt"

    check_and_record_daily_use(user["id"])

    with db() as con:
        con.execute(
            """
            INSERT INTO jobs(id,user_id,filename,status,created_at)
            VALUES(?,?,?,?,?)
            """,
            (
                job,
                user["id"],
                original_filename,
                "processing",
                datetime.now(timezone.utc).isoformat()
            )
        )

    try:
        run_ffmpeg(
            [
                "-i", str(source),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "64k",
                str(audio)
            ]
        )

        transcript_data = await transcribe(audio)
        transcript = (transcript_data.get("text") or "").strip()

        if not transcript:
            raise HTTPException(422, "အသံမှ စာသားမရပါ။")

        target = {
            "my": "Burmese",
            "en": "English",
            "th": "Thai"
        }.get(language, "Burmese")

        if mode == "pro":
            detail = "Provide a detailed structured recap with headings and key points."
        elif mode == "subtitle":
            detail = "Provide a brief description of the video topic and key context."
        else:
            detail = "Provide a concise recap with key bullet points."

        recap_text = await ai_text(
            (
                f"You summarize video transcripts in {target}. "
                f"Be faithful to the transcript. Do not invent facts. {detail}"
            ),
            "Summarize this transcript:\n\n" + transcript[:90000]
        )

        recapfile.write_text(recap_text, encoding="utf-8")

        srt.write_text(
            make_srt(transcript_data.get("segments", [])),
            encoding="utf-8"
        )

        run_ffmpeg(
            [
                "-i", str(source),
                "-filter_complex", format_filter(ratio, fit_mode),
                "-map", "[v]",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output)
            ],
            timeout=2400
        )

        result = {
            "job_id": job,
            "recap": recap_text,
            "transcript": transcript[:10000],
            "downloads": {
                "video": f"/api/download/{job}/video",
                "srt": f"/api/download/{job}/srt",
                "text": f"/api/download/{job}/text"
            }
        }

        if mode == "pro":
            highlight, error = await make_highlights(
                transcript,
                transcript_data.get("segments", []),
                folder,
                ratio,
                fit_mode
            )

            if highlight:
                result["downloads"]["highlights"] = (
                    f"/api/download/{job}/highlights"
                )
            else:
                result["highlight_note"] = error

        with db() as con:
            con.execute(
                "UPDATE jobs SET status='done' WHERE id=?",
                (job,)
            )

        return result

    except Exception:
        with db() as con:
            con.execute(
                "UPDATE jobs SET status='failed' WHERE id=?",
                (job,)
            )
        raise


# --------------------------------------------------
# PWA icons, manifest and website
# --------------------------------------------------

@app.get("/icon-192.png")
async def icon_192():
    path = PUBLIC / "icon-192.png"

    if not path.is_file():
        raise HTTPException(404, "192 icon မတွေ့ပါ။")

    return FileResponse(path, media_type="image/png")


@app.get("/icon-512.png")
async def icon_512():
    path = PUBLIC / "icon-512.png"

    if not path.is_file():
        raise HTTPException(404, "512 icon မတွေ့ပါ။")

    return FileResponse(path, media_type="image/png")


@app.get("/manifest.json")
async def pwa_manifest():
    path = PUBLIC / "manifest.json"

    if not path.is_file():
        raise HTTPException(404, "manifest.json မတွေ့ပါ။")

    return FileResponse(
        path, media_type="application/manifest+json"
    )


@app.get("/sw.js")
async def pwa_service_worker():
    path = PUBLIC / "sw.js"

    if not path.is_file():
        raise HTTPException(404, "sw.js မတွေ့ပါ။")

    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"}
    )


@app.get("/", response_class=HTMLResponse)
async def home():
    return (PUBLIC / "index.html").read_text(encoding="utf-8")


@app.get("/sitemap.xml")
async def sitemap():
    path = PUBLIC / "sitemap.xml"

    if not path.is_file():
        raise HTTPException(404, "sitemap.xml မတွေ့ပါ။")

    return FileResponse(path, media_type="application/xml")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "ai_configured": bool(API_KEY)
    }


# --------------------------------------------------
# Register, login and account
# --------------------------------------------------

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
        raise HTTPException(409, "ဤ email ကို အသုံးပြု၍မရပါ။")

    with db() as con:
        try:
            cur = con.execute(
                """
                INSERT INTO users(
                    email,password,role,credits,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    email,
                    pw_hash(password),
                    "user",
                    STARTER_CREDITS,
                    datetime.now(timezone.utc).isoformat()
                )
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                409, "ဤ email ဖြင့် အကောင့်ရှိပြီးသားပါ။"
            )

        uid = cur.lastrowid

        user = con.execute(
            """
            SELECT id,email,role,credits,is_premium
            FROM users WHERE id=?
            """,
            (uid,)
        ).fetchone()

    return {
        "token": token_for(user),
        "user": dict(user)
    }


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
        raise HTTPException(
            401, "Email သို့မဟုတ် Password မှားနေပါသည်။"
        )

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


# --------------------------------------------------
# Video upload endpoint
# --------------------------------------------------

@app.post("/api/recap")
async def recap(
    file: UploadFile = File(...),
    language: str = Form("my"),
    mode: str = Form("recap"),
    ratio: str = Form("9:16"),
    fit_mode: str = Form("crop"),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    suffix = Path(file.filename or "").suffix.lower()

    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            415, "MP4, MOV, MKV, WEBM ဖိုင်ကိုသာ တင်ပါ။"
        )

    if mode not in ("recap", "pro", "subtitle"):
        raise HTTPException(400, "Mode မမှန်ပါ။")

    if ratio not in ("9:16", "3:4", "16:9", "1:1"):
        raise HTTPException(400, "Video ratio မမှန်ပါ။")

    if fit_mode not in ("crop", "blur"):
        raise HTTPException(400, "Fit mode မမှန်ပါ။")

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
                        f"ဖိုင်အရွယ်အစား {MAX_UPLOAD_MB}MB ထက် မကျော်ရပါ။"
                    )

                output_file.write(chunk)

        if size == 0:
            raise HTTPException(400, "Video ဖိုင်အလွတ် ဖြစ်နေပါသည်။")

        return await process_video(
            source=source,
            original_filename=file.filename or "uploaded-video",
            user=user,
            language=language,
            mode=mode,
            ratio=ratio,
            fit_mode=fit_mode,
            job=job,
            folder=folder
        )

    finally:
        await file.close()


# --------------------------------------------------
# YouTube / TikTok URL endpoint
# Frontend sends FormData field: url
# --------------------------------------------------

@app.post("/api/recap-url")
async def recap_url(
    url: str = Form(...),
    language: str = Form("my"),
    mode: str = Form("recap"),
    ratio: str = Form("9:16"),
    fit_mode: str = Form("crop"),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    if mode not in ("recap", "pro", "subtitle"):
        raise HTTPException(400, "Mode မမှန်ပါ။")

    if ratio not in ("9:16", "3:4", "16:9", "1:1"):
        raise HTTPException(400, "Video ratio မမှန်ပါ။")

    if fit_mode not in ("crop", "blur"):
        raise HTTPException(400, "Fit mode မမှန်ပါ။")

    safe_url = validate_video_url(url)

    job = uuid.uuid4().hex
    folder = DATA / job
    folder.mkdir(parents=True, exist_ok=True)

    try:
        # Download public video using yt-dlp.
        source = download_public_video(safe_url, folder)

        return await process_video(
            source=source,
            original_filename="video-from-url" + source.suffix,
            user=user,
            language=language,
            mode=mode,
            ratio=ratio,
            fit_mode=fit_mode,
            job=job,
            folder=folder
        )

    except Exception:
        # Remove failed URL download files to save storage.
        # Keep job folder if processing already created a job record.
        with db() as con:
            existing = con.execute(
                "SELECT id FROM jobs WHERE id=?",
                (job,)
            ).fetchone()

        if not existing:
            shutil.rmtree(folder, ignore_errors=True)

        raise


# --------------------------------------------------
# Download generated files
# --------------------------------------------------

@app.get("/api/download/{job_id}/{kind}")
async def download(
    job_id: str,
    kind: str,
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Invalid job id")

    with db() as con:
        job = con.execute(
            "SELECT user_id FROM jobs WHERE id=?",
            (job_id,)
        ).fetchone()

    if not job or (
        job["user_id"] != user["id"]
        and user["role"] != "admin"
    ):
        raise HTTPException(404, "ဖိုင်မတွေ့ပါ။")

    folder = DATA / job_id

    options = {
        "video": (
            folder / "formatted.mp4",
            "formatted-video.mp4"
        ),
        "srt": (
            folder / "subtitles.srt",
            "subtitles.srt"
        ),
        "text": (
            folder / "recap.txt",
            "video-recap.txt"
        ),
        "highlights": (
            folder / "highlights.mp4",
            "ai-highlights.mp4"
        )
    }

    if kind not in options:
        raise HTTPException(404, "Not found")

    path, filename = options[kind]

    if not path.exists():
        raise HTTPException(404, "ဖိုင်မတွေ့ပါ။")

    return FileResponse(path, filename=filename)


# --------------------------------------------------
# Admin dashboard
# --------------------------------------------------

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
        "users": [dict(item) for item in users],
        "jobs": [dict(item) for item in jobs]
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
            400, "Credit ပမာဏ 1 မှ 10000 အတွင်း ဖြစ်ရပါမည်။"
        )

    with db() as con:
        target = con.execute(
            "SELECT id FROM users WHERE id=?",
            (user_id,)
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
            (
                user_id,
                amount,
                "Admin credit adjustment",
                datetime.now(timezone.utc).isoformat()
            )
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
            400, "enabled သည် 0 သို့မဟုတ် 1 ဖြစ်ရပါမည်။"
        )

    with db() as con:
        target = con.execute(
            "SELECT id FROM users WHERE id=?",
            (user_id,)
        ).fetchone()

        if not target:
            raise HTTPException(404, "User မတွေ့ပါ။")

        con.execute(
            "UPDATE users SET is_premium=? WHERE id=?",
            (enabled, user_id)
        )

    return {
        "ok": True,
        "is_premium": bool(enabled)
    }
