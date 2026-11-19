import os
import uuid
import shutil
import secrets
import hashlib
import hmac
import ipaddress
import socket
import subprocess
import sqlite3
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone

import httpx
import yt_dlp
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "deepclip.sqlite3"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE = "https://api.openai.com/v1"
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_DEFAULT_VOICE = os.getenv("TTS_VOICE", "alloy")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
MAX_VIDEO_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_AUDIO_MB = 100
STARTER_CREDITS = int(os.getenv("STARTER_CREDITS", "3"))
PREMIUM_PRICE_KS = int(os.getenv("PREMIUM_PRICE_KS", "20000"))
ALLOWED_TTS_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm", ".ogg", ".flac"}
ALLOWED_LANGUAGES = {"my": "Burmese", "en": "English", "th": "Thai"}
ALLOWED_RATIOS = {"9:16", "3:4", "16:9", "1:1"}
ALLOWED_FIT = {"crop", "blur", "contain"}
app = FastAPI(title="DeepClip AI Video Platform")
JOBS = {}

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            credits INTEGER NOT NULL DEFAULT 3, is_premium INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage(
            user_id INTEGER NOT NULL, usage_day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,usage_day), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, folder TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
init_db()

def qr_file():
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ""):
        p = DATA_DIR / ("kpay-qr" + suffix)
        if p.is_file():
            return p
    return None

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240000)
    return salt.hex() + "$" + digest.hex()

def verify_password(password, stored):
    try:
        salt, expected = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def user_payload(row):
    return {"id": row["id"], "email": row["email"], "role": row["role"],
            "credits": row["credits"], "is_premium": bool(row["is_premium"])}

def create_session(user_id):
    token = secrets.token_urlsafe(40)
    with db() as conn:
        conn.execute("INSERT INTO sessions(token_hash,user_id) VALUES(?,?)", (token_digest(token), user_id))
    return token

def current_user(authorization):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Login required")
    token = authorization.split(" ", 1)[1].strip()
    with db() as conn:
        row = conn.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                              WHERE s.token_hash=?""", (token_digest(token),)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid session")
    return dict(row)

def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")

def require_api_key():
    if not OPENAI_API_KEY:
        raise HTTPException(503, "OPENAI_API_KEY is not configured in Render Environment")

def clean_error(error):
    msg = str(error)
    low = msg.lower()
    if "api_key" in low or "incorrect api key" in low or "invalid_api_key" in low:
        return "OpenAI API Key မမှန်ပါ။ Render Environment မှာ မှန်ကန်တဲ့ Key ကို စစ်ဆေးပါ။"
    return msg[:700]

async def ai_text(system, prompt):
    require_api_key()
    async with httpx.AsyncClient(timeout=240) as client:
        r = await client.post(f"{OPENAI_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": TEXT_MODEL, "messages":[{"role":"system","content":system},
                  {"role":"user","content":prompt}]})
    if r.status_code != 200:
        raise RuntimeError("AI text request failed: " + r.text[:300])
    return r.json()["choices"][0]["message"]["content"].strip()

async def transcribe(path):
    require_api_key()
    async with httpx.AsyncClient(timeout=900) as client:
        with path.open("rb") as f:
            r = await client.post(f"{OPENAI_BASE}/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data={"model": TRANSCRIBE_MODEL, "response_format":"verbose_json"},
                files={"file":(path.name,f,"application/octet-stream")})
    if r.status_code != 200:
        raise RuntimeError("Transcription failed: " + r.text[:300])
    return r.json()

async def translate_text(text, language="my"):
    if language not in ALLOWED_LANGUAGES:
        raise HTTPException(400, "Unsupported language")
    return await ai_text(f"Translate accurately into {ALLOWED_LANGUAGES[language]}. Return only the translation.", text)

async def generate_speech(text, output, voice=None):
    require_api_key()
    voice = voice or TTS_DEFAULT_VOICE
    if voice not in ALLOWED_TTS_VOICES:
        raise HTTPException(400, "Unsupported TTS voice")
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{OPENAI_BASE}/audio/speech",
            headers={"Authorization":f"Bearer {OPENAI_API_KEY}"},
            json={"model":TTS_MODEL,"voice":voice,"input":text[:10000],"response_format":"mp3"})
    if r.status_code != 200:
        raise RuntimeError("Speech generation failed: " + r.text[:300])
    output.write_bytes(r.content)
    return output

def validate_video_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http","https") or not parsed.hostname:
        raise RuntimeError("မှန်ကန်တဲ့ HTTP/HTTPS Video URL ထည့်ပါ။")
    host = parsed.hostname.lower()
    if host in ("localhost","localhost.localdomain"):
        raise RuntimeError("Local URL များကို ခွင့်မပြုပါ။")
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not addresses:
            raise RuntimeError("Website address ကို ရှာမတွေ့ပါ။")
        for item in addresses:
            if not ipaddress.ip_address(item[4][0]).is_global:
                raise RuntimeError("Private/local address များကို ခွင့်မပြုပါ။")
    except socket.gaierror:
        raise RuntimeError("Website address ကို ရှာမတွေ့ပါ။")
    return True

def download_video(url, folder):
    validate_video_url(url)
    template = str(folder / "source.%(ext)s")
    opts = {"outtmpl":template,"format":"best[height<=1080]/best",
        "noplaylist":True,"max_filesize":MAX_VIDEO_MB*1024*1024,
        "quiet":True,"no_warnings":True,"socket_timeout":30,"retries":2}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in folder.glob("source.*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError("Video download မအောင်မြင်ပါ။")
    return files[0]

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None

def run_ffmpeg(args, timeout=1800):
    if not ffmpeg_available():
        raise RuntimeError("Server မှာ FFmpeg မတပ်ဆင်ရသေးပါ။ Render မှာ FFmpeg ထည့်သွင်းပါ။")
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y"] + [str(x) for x in args],
                   check=True, capture_output=True, timeout=timeout)

def extract_audio(video, folder):
    audio = folder / "audio.mp3"
    run_ffmpeg(["-i",video,"-vn","-ac","1","-ar","16000","-b:a","64k",audio])
    if not audio.exists() or audio.stat().st_size > MAX_AUDIO_MB*1024*1024:
        raise RuntimeError("Audio ဖိုင်အရွယ်အစား မမှန်ပါ။")
    return audio

def make_srt(segments, output):
    def stamp(value):
        ms=max(0,int(float(value or 0)*1000))
        return f"{ms//3600000:02}:{(ms%3600000)//60000:02}:{(ms%60000)//1000:02},{ms%1000:03}"
    with output.open("w",encoding="utf-8") as f:
        for i,seg in enumerate(segments,1):
            f.write(f"{i}\n{stamp(seg.get('start'))} --> {stamp(seg.get('end'))}\n{seg.get('text','').strip()}\n\n")
    return output

def export_video(video, folder, ratio="9:16", fit_mode="crop", burn_subtitles=False):
    if ratio not in ALLOWED_RATIOS:
        ratio="9:16"
    if fit_mode not in ALLOWED_FIT:
        fit_mode="crop"
    dims={"9:16":(1080,1920),"3:4":(1080,1440),"16:9":(1920,1080),"1:1":(1080,1080)}
    w,h=dims[ratio]
    if fit_mode=="crop":
        vf=f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    elif fit_mode=="blur":
        vf=(f"[0:v]split=2[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:10[bg2];[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1")
    else:
        vf=f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    output=folder/"export.mp4"
    args=["-i",video]
    if fit_mode=="blur":
        args += ["-filter_complex",vf]
    else:
        args += ["-vf",vf]
    if burn_subtitles and (folder/"subtitles.srt").exists():
        # Escape path for FFmpeg subtitles filter.
        sub=str(folder/"subtitles.srt").replace("\\","/").replace(":","\\:").replace("'","\\'")
        if fit_mode=="blur":
            args[-1] += f",subtitles='{sub}'"
        else:
            args[args.index("-vf")+1] += f",subtitles='{sub}'"
    args += ["-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart",output]
    run_ffmpeg(args,timeout=3600)
    if not output.exists():
        raise RuntimeError("Video export မအောင်မြင်ပါ။")
    return output

def consume_credit(user_id):
    today=datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row=conn.execute("SELECT credits,is_premium FROM users WHERE id=?",(user_id,)).fetchone()
        if not row:
            raise HTTPException(401,"User not found")
        if row["is_premium"]:
            return
        if row["credits"]>0:
            conn.execute("UPDATE users SET credits=credits-1 WHERE id=?",(user_id,))
            return
        used=conn.execute("SELECT count FROM usage WHERE user_id=? AND usage_day=?",(user_id,today)).fetchone()
        count=used["count"] if used else 0
        if count>=STARTER_CREDITS:
            raise HTTPException(402,"ဒီနေ့ Free အသုံးပြုခွင့် ပြည့်သွားပါပြီ။ Premium အတွက် Admin ကို ဆက်သွယ်ပါ။")
        conn.execute("""INSERT INTO usage(user_id,usage_day,count) VALUES(?,?,1)
                        ON CONFLICT(user_id,usage_day) DO UPDATE SET count=count+1""",(user_id,today))

def refund_credit(user_id):
    with db() as conn:
        conn.execute("UPDATE users SET credits=credits+1 WHERE id=? AND is_premium=0",(user_id,))

def user_after_update(user_id):
    with db() as conn:
        row=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    return user_payload(row) if row else None

async def run_recap(video,folder,user,language,mode,ratio,fit_mode):
    consume_credit(user["id"])
    try:
        audio=await asyncio.to_thread(extract_audio,video,folder)
        transcript=await transcribe(audio)
        transcript_text=transcript.get("text","").strip()
        segments=transcript.get("segments",[])
        if not transcript_text:
            raise RuntimeError("ဒီ Video မှာ စကားသံ မတွေ့ပါ။")
        original=await ai_text("You are an expert video summarizer. Give a structured, accurate recap with headings, key points and conclusion. Do not invent facts.",transcript_text)
        lang=language if language in ALLOWED_LANGUAGES else "my"
        translated=await translate_text(transcript_text,lang)
        localized=await ai_text("Summarize faithfully. Do not add facts.",
            f"Write a clear structured summary in {ALLOWED_LANGUAGES[lang]}. Use headings and key points. Source recap:\n{original}")
        srt=folder/"subtitles.srt"
        make_srt(segments,srt)
        (folder/"recap.txt").write_text("AI VIDEO RECAP\n\n"+original+"\n\nLOCALIZED RECAP\n\n"+localized+
            "\n\nTRANSCRIPT / TRANSLATION\n\n"+translated,encoding="utf-8")
        # Export is an actual processed video; subtitle burn-in is available for subtitle mode.
        exported=await asyncio.to_thread(export_video,video,folder,ratio,fit_mode,mode=="subtitle")
        job_id=folder.name
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO jobs(id,user_id,folder,status) VALUES(?,?,?,'completed')",
                         (job_id,user["id"],str(folder)))
        JOBS[job_id]={"user_id":user["id"],"folder":str(folder),"status":"completed"}
        return {"success":True,"recap":localized,"original_recap":original,
            "transcript":transcript_text,"translation":translated,
            "downloads":{"text":f"/api/jobs/{job_id}/download/recap",
                         "srt":f"/api/jobs/{job_id}/download/subtitles",
                         "video":f"/api/jobs/{job_id}/download/video"},
            "highlight_note":("Pro Recap: အကျဉ်းချုပ်နှင့် Video Export ပါဝင်ပါတယ်။ သီးခြား Highlight Clip ရွေးထုတ်ခြင်း မပါသေးပါ။" if mode=="pro" else ""),
            "user":user_after_update(user["id"])}
    except Exception:
        refund_credit(user["id"])
        raise

async def save_upload(upload,folder):
    ext=Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_VIDEO_EXT:
        raise HTTPException(400,"Unsupported video format")
    target=folder/("source"+ext)
    size=0
    with target.open("wb") as out:
        while True:
            chunk=await upload.read(1024*1024)
            if not chunk: break
            size+=len(chunk)
            if size>MAX_VIDEO_MB*1024*1024:
                raise HTTPException(413,f"Video size limit is {MAX_VIDEO_MB} MB")
            out.write(chunk)
    return target

@app.get("/",response_class=HTMLResponse)
async def home():
    index=BASE_DIR.parent/"public"/"index.html"
    if not index.exists(): raise HTTPException(404,"DeepClip frontend not found")
    return HTMLResponse(index.read_text(encoding="utf-8"))

@app.get("/health")
async def health():
    return {"status":"ok","ffmpeg":ffmpeg_available()}

@app.post("/api/register")
async def register(email:str=Form(...),password:str=Form(...)):
    email=email.strip().lower()
    if len(email)>254 or "@" not in email: raise HTTPException(400,"မှန်ကန်သော Email ထည့်ပါ။")
    if len(password)<8: raise HTTPException(400,"Password အနည်းဆုံး ၈ လုံး လိုအပ်ပါတယ်။")
    with db() as conn:
        if conn.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone():
            raise HTTPException(409,"Account already exists")
        role="admin" if ADMIN_EMAIL and ADMIN_PASSWORD and email==ADMIN_EMAIL and hmac.compare_digest(password,ADMIN_PASSWORD) else "user"
        cur=conn.execute("INSERT INTO users(email,password_hash,role,credits) VALUES(?,?,?,?)",
                         (email,hash_password(password),role,STARTER_CREDITS))
        uid=cur.lastrowid
    return {"success":True,"token":create_session(uid),"user":user_after_update(uid)}

@app.post("/api/login")
async def login(email:str=Form(...),password:str=Form(...)):
    email=email.strip().lower()
    with db() as conn: row=conn.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
    if not row or not verify_password(password,row["password_hash"]): raise HTTPException(401,"Incorrect email or password")
    if ADMIN_EMAIL and email==ADMIN_EMAIL and ADMIN_PASSWORD and hmac.compare_digest(password,ADMIN_PASSWORD):
        with db() as conn: conn.execute("UPDATE users SET role='admin' WHERE id=?",(row["id"],))
    return {"success":True,"token":create_session(row["id"]),"user":user_after_update(row["id"])}

@app.post("/api/logout")
async def logout(authorization:str|None=Header(default=None)):
    current_user(authorization)
    token=authorization.split(" ",1)[1].strip()
    with db() as conn: conn.execute("DELETE FROM sessions WHERE token_hash=?",(token_digest(token),))
    return {"success":True}

@app.get("/api/me")
async def me(authorization:str|None=Header(default=None)):
    user=current_user(authorization)
    return {"user":user_payload(user)}

@app.post("/api/recap")
async def recap_upload(file:UploadFile=File(...),language:str=Form("my"),mode:str=Form("recap"),
    ratio:str=Form("9:16"),fit_mode:str=Form("crop"),authorization:str|None=Header(default=None)):
    user=current_user(authorization)
    folder=DATA_DIR/f"job_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)
    try:
        video=await save_upload(file,folder)
        return await run_recap(video,folder,user,language,mode,ratio,fit_mode)
    except HTTPException:
        shutil.rmtree(folder,ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True); raise HTTPException(500,clean_error(e))

@app.post("/api/recap-url")
async def recap_url(url:str=Form(...),language:str=Form("my"),mode:str=Form("recap"),
    ratio:str=Form("9:16"),fit_mode:str=Form("crop"),authorization:str|None=Header(default=None)):
    user=current_user(authorization)
    folder=DATA_DIR/f"job_{uuid.uuid4().hex}"
    folder.mkdir(parents=True)
    try:
        video=await asyncio.to_thread(download_video,url,folder)
        return await run_recap(video,folder,user,language,mode,ratio,fit_mode)
    except HTTPException:
        shutil.rmtree(folder,ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True); raise HTTPException(400,clean_error(e))

@app.get("/api/jobs/{job_id}")
async def job_status(job_id:str,authorization:str|None=Header(default=None)):
    user=current_user(authorization)
    with db() as conn: job=conn.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user["id"])).fetchone()
    if not job: raise HTTPException(404,"Job not found")
    return {"job_id":job_id,"status":job["status"],"progress":100,"message":"completed"}

@app.get("/api/jobs/{job_id}/download/{kind}")
async def download_result(job_id:str,kind:str,authorization:str|None=Header(default=None)):
    user=current_user(authorization)
    with db() as conn: job=conn.execute("SELECT * FROM jobs WHERE id=? AND user_id=?",(job_id,user["id"])).fetchone()
    if not job: raise HTTPException(404,"File not found")
    names={"recap":("recap.txt","text/plain"),"subtitles":("subtitles.srt","application/x-subrip"),
           "video":("export.mp4","video/mp4")}
    if kind not in names: raise HTTPException(404,"File not found")
    filename,media=names[kind]
    target=Path(job["folder"])/filename
    if not target.exists(): raise HTTPException(404,"File not found")
    return FileResponse(target,media_type=media,filename=filename)

@app.post("/api/transcribe")
async def api_transcribe(file:UploadFile=File(...),authorization:str|None=Header(default=None)):
    current_user(authorization)
    ext=Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO_EXT: raise HTTPException(400,"Unsupported audio format")
    folder=DATA_DIR/f"transcribe_{uuid.uuid4().hex}"; folder.mkdir()
    try:
        target=folder/("audio"+ext); size=0
        with target.open("wb") as out:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>MAX_AUDIO_MB*1024*1024: raise HTTPException(413,"Audio too large")
                out.write(chunk)
        result=await transcribe(target)
        return {"success":True,"text":result.get("text",""),"language":result.get("language"),"segments":result.get("segments",[])}
    finally: shutil.rmtree(folder,ignore_errors=True)

@app.post("/api/translate")
async def api_translate(text:str=Form(...),target_language:str=Form("my"),authorization:str|None=Header(default=None)):
    current_user(authorization)
    if not text.strip() or len(text)>50000: raise HTTPException(400,"Text is empty or too long")
    result=await translate_text(text,target_language)
    return {"success":True,"translation":result,"translated_text":result}

@app.post("/api/tts")
async def api_tts(text:str=Form(...),voice:str=Form(TTS_DEFAULT_VOICE),authorization:str|None=Header(default=None)):
    current_user(authorization)
    if not text.strip() or len(text)>10000: raise HTTPException(400,"Invalid text length")
    folder=DATA_DIR/f"tts_{uuid.uuid4().hex}"; folder.mkdir()
    output=folder/"speech.mp3"
    try:
        await generate_speech(text,output,voice)
        return FileResponse(output,media_type="audio/mpeg",filename="deepclip-speech.mp3",
                            background=BackgroundTask(shutil.rmtree,folder,ignore_errors=True))
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True); raise HTTPException(500,clean_error(e))

@app.get("/api/payment-qr")
async def public_payment_qr():
    path=qr_file()
    if not path: raise HTTPException(404,"KPay QR not uploaded")
    media={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp"}.get(path.suffix,"application/octet-stream")
    return FileResponse(path,media_type=media,filename="kpay-qr"+path.suffix)

@app.get("/api/admin/payment-qr")
async def admin_get_qr(authorization:str|None=Header(default=None)):
    user=current_user(authorization); require_admin(user)
    path=qr_file()
    if not path: raise HTTPException(404,"KPay QR not uploaded")
    return FileResponse(path,filename=path.name)

@app.post("/api/admin/payment-qr")
async def admin_upload_qr(file:UploadFile=File(...),authorization:str|None=Header(default=None)):
    user=current_user(authorization); require_admin(user)
    ctype=file.content_type or ""
    types={"image/png":".png","image/jpeg":".jpg","image/webp":".webp"}
    if ctype not in types: raise HTTPException(400,"PNG, JPG, WEBP ပုံသာ ခွင့်ပြုပါတယ်။")
    data=await file.read(5*1024*1024+1)
    if len(data)>5*1024*1024: raise HTTPException(413,"QR image must be 5MB or smaller")
    # Verify actual image signature rather than trusting the browser MIME type.
    valid=(ctype=="image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or (ctype=="image/jpeg" and data.startswith(b"\xff\xd8\xff")) or (ctype=="image/webp" and data.startswith(b"RIFF") and data[8:12]==b"WEBP")
    if not valid: raise HTTPException(400,"Image file format မမှန်ပါ။")
    for p in DATA_DIR.glob("kpay-qr*"):
        if p.is_file(): p.unlink()
    target=DATA_DIR/("kpay-qr"+types[ctype]); target.write_bytes(data)
    return {"success":True,"message":"KPay QR Upload အောင်မြင်ပါပြီ။"}

@app.get("/api/admin/overview")
async def admin_overview(authorization:str|None=Header(default=None)):
    user=current_user(authorization); require_admin(user)
    with db() as conn:
        users=conn.execute("SELECT id,email,role,credits,is_premium FROM users ORDER BY id DESC").fetchall()
        jobs=conn.execute("SELECT id,user_id,status,created_at FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"users":[dict(r)|{"is_premium":bool(r["is_premium"])} for r in users],"jobs":[dict(r) for r in jobs]}

@app.post("/api/admin/credits")
async def admin_credits(user_id:int=Form(...),amount:int=Form(...),authorization:str|None=Header(default=None)):
    admin=current_user(authorization); require_admin(admin)
    if amount==0 or abs(amount)>100000: raise HTTPException(400,"Credit amount မမှန်ပါ။")
    with db() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone(): raise HTTPException(404,"User not found")
        conn.execute("UPDATE users SET credits=MAX(0,credits+?) WHERE id=?",(amount,user_id))
    return {"success":True,"user":user_after_update(user_id)}

@app.post("/api/admin/premium")
async def admin_premium(user_id:int=Form(...),enabled:int=Form(...),authorization:str|None=Header(default=None)):
    admin=current_user(authorization); require_admin(admin)
    if enabled not in (0,1): raise HTTPException(400,"Invalid premium value")
    with db() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone(): raise HTTPException(404,"User not found")
        conn.execute("UPDATE users SET is_premium=? WHERE id=?",(enabled,user_id))
    return {"success":True,"user":user_after_update(user_id)}

@app.get("/api/config")
async def public_config():
    return {"premium_price_ks":PREMIUM_PRICE_KS,"starter_credits":STARTER_CREDITS,
            "max_upload_mb":MAX_VIDEO_MB,"tts_voices":sorted(ALLOWED_TTS_VOICES),
            "ratios":sorted(ALLOWED_RATIOS),"fit_modes":sorted(ALLOWED_FIT)}
