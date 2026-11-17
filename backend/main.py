import os, re, uuid, json, hmac, hashlib, secrets, sqlite3, shutil, subprocess, time
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse
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

app = FastAPI(title="DeepClip Mobile MVP")
app.mount("/public", StaticFiles(directory=str(PUBLIC)), name="public")

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def pw_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180000).hex()
    return salt + "$" + digest

def pw_check(password, stored):
    try:
        salt, digest = stored.split("$", 1)
        return hmac.compare_digest(pw_hash(password, salt).split("$",1)[1], digest)
    except Exception:
        return False

def token_for(user):
    payload = json.dumps({"uid": user["id"], "exp": int(time.time()) + 60*60*24*14}, separators=(",", ":")).encode()
    body = secrets.base64.urlsafe_b64encode(payload).decode().rstrip("=") if hasattr(secrets, "base64") else __import__("base64").urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig

def read_token(token):
    import base64
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
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
        raise HTTPException(401, "Session သက်တမ်းကုန်သွားပါပြီ။ ပြန်ဝင်ပါ။")
    with db() as con:
        user = con.execute("SELECT id,email,role,credits,is_premium FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        raise HTTPException(401, "အကောင့်မတွေ့ပါ။")
    return dict(user)

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            credits INTEGER NOT NULL DEFAULT 0,
            is_premium INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
        # Lightweight migration for databases created by earlier versions.
        cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "is_premium" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, filename TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        existing = con.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
        if not existing:
            cur = con.execute("INSERT INTO users(email,password,role,credits,created_at) VALUES(?,?,?,?,?)",
                (ADMIN_EMAIL, pw_hash(ADMIN_PASSWORD), "admin", 999999, datetime.now(timezone.utc).isoformat()))
            con.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                (cur.lastrowid, 999999, "Admin initial balance", datetime.now(timezone.utc).isoformat()))
init_db()

def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin သာ အသုံးပြုနိုင်ပါသည်။")


def myanmar_today():
    # Myanmar time is UTC+06:30. Use a fixed offset so server timezone does not matter.
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=6, minutes=30)).date().isoformat()

def check_and_record_daily_use(uid):
    today = myanmar_today()
    with db() as con:
        row = con.execute("SELECT is_premium FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(401, "အကောင့်မတွေ့ပါ။")
        if not row["is_premium"]:
            used = con.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND status IN ('processing','done') AND substr(created_at,1,10)=?",
                (uid, today)
            ).fetchone()["n"]
            if used >= 3:
                raise HTTPException(402, "ဒီနေ့ Free အသုံးပြုခွင့် ၃ ကြိမ် ပြည့်သွားပါပြီ။ ဆက်သုံးရန် Premium ဝယ်ယူပါ။")
        return bool(row["is_premium"])

def run_ffmpeg(args, timeout=3600):
    try:
        p = subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error"] + args,
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(500, "FFmpeg မတွေ့ပါ။ FFmpeg install လုပ်ပါ။")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Video processing အချိန်ကုန်သွားပါတယ်။")
    if p.returncode:
        raise HTTPException(400, "Video ကို process မလုပ်နိုင်ပါ။ Format ကို စစ်ပါ။")
    return p

def srt_time(seconds):
    ms = int(max(0, float(seconds))*1000)
    h, ms = divmod(ms,3600000); m, ms = divmod(ms,60000); s, ms = divmod(ms,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def make_srt(segments):
    out=[]
    for i, seg in enumerate(segments,1):
        txt=(seg.get("text") or "").strip()
        if txt:
            out.append(f"{i}\n{srt_time(seg.get('start',0))} --> {srt_time(seg.get('end',0))}\n{txt}\n")
    return "\n".join(out)

async def transcribe(path):
    if not API_KEY:
        raise HTTPException(503, "AI API key မထည့်ရသေးပါ။ backend/.env ကို ပြင်ပါ။")
    headers={"Authorization":f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=900) as client:
        with path.open("rb") as f:
            r=await client.post("https://api.openai.com/v1/audio/transcriptions",
                headers=headers, data={"model":TRANSCRIBE_MODEL,"response_format":"verbose_json"},
                files={"file":(path.name,f,"audio/mpeg")})
    if r.status_code>=400:
        raise HTTPException(502, "AI transcription မအောင်မြင်ပါ။ API key, billing နှင့် ဖိုင်အရွယ်အစားကို စစ်ပါ။")
    return r.json()

async def ai_text(system, prompt):
    headers={"Authorization":f"Bearer {API_KEY}","Content-Type":"application/json"}
    payload={"model":TEXT_MODEL,"messages":[{"role":"system","content":system},{"role":"user","content":prompt}],"temperature":0.25}
    async with httpx.AsyncClient(timeout=240) as client:
        r=await client.post("https://api.openai.com/v1/chat/completions",headers=headers,json=payload)
    if r.status_code>=400:
        raise HTTPException(502, "AI recap မအောင်မြင်ပါ။ Text model/API ခွင့်ပြုချက်ကို စစ်ပါ။")
    return r.json()["choices"][0]["message"]["content"]

def dimensions(ratio):
    return {"9:16":(720,1280),"3:4":(900,1200),"16:9":(1280,720),"1:1":(1080,1080)}.get(ratio,(720,1280))

def format_filter(ratio, fit):
    w,h=dimensions(ratio)
    if fit=="blur":
        return f"[0:v]split=2[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:12[blur];[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[front];[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    return f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[v]"

async def make_highlights(transcript, segments, folder, ratio, fit):
    # Ask AI for timestamp ranges grounded in transcript; clamp and validate them.
    if not segments:
        return None, "အသံမှ timestamp segment မရသောကြောင့် highlight clip မဖန်တီးနိုင်ပါ။"
    compact=[]
    for s in segments[:500]:
        compact.append({"start":s.get("start",0),"end":s.get("end",0),"text":(s.get("text") or "")[:240]})
    raw=await ai_text(
        "You select useful, self-contained highlight moments from a video transcript. Return ONLY valid JSON: {\"clips\":[{\"start\":number,\"end\":number,\"reason\":\"short reason\"}]}. Select 3 to 6 moments. Each should be 8 to 35 seconds, grounded in the supplied segments, non-overlapping where possible. Do not invent timestamps.",
        "Transcript segments:\n"+json.dumps(compact,ensure_ascii=False)[:60000]
    )
    try:
        match=re.search(r"\{.*\}",raw,re.S)
        clips=json.loads(match.group(0))["clips"] if match else []
    except Exception:
        clips=[]
    valid=[]
    for c in clips:
        try:
            start=max(0,float(c["start"])); end=float(c["end"])
            if 6 <= end-start <= 45:
                valid.append((start,end))
        except Exception:
            pass
    valid=valid[:6]
    if not valid:
        return None, "AI က အသုံးပြုနိုင်သော highlight timestamp မရွေးနိုင်ပါ။"
    source=folder/"source_video"
    pieces=[]
    for i,(start,end) in enumerate(valid):
        piece=folder/f"clip_{i}.mp4"
        run_ffmpeg(["-ss",str(start),"-to",str(end),"-i",str(source),"-c","copy",str(piece)],timeout=600)
        if piece.exists() and piece.stat().st_size>0: pieces.append(piece)
    if not pieces:
        return None, "Highlight clip များ ဖန်တီးမရပါ။"
    listfile=folder/"concat.txt"
    listfile.write_text("".join("file '"+p.as_posix().replace("'","'\\''")+"'\n" for p in pieces),encoding="utf-8")
    joined=folder/"joined.mp4"
    run_ffmpeg(["-f","concat","-safe","0","-i",str(listfile),"-c","copy",str(joined)],timeout=1200)
    out=folder/"highlights.mp4"
    vf=format_filter(ratio,fit)
    run_ffmpeg(["-i",str(joined),"-filter_complex",vf,"-map","[v]","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-movflags","+faststart",str(out)],timeout=1800)
    return out, None

@app.get("/", response_class=HTMLResponse)
async def home():
    return (PUBLIC / "index.html").read_text(encoding="utf-8")


@app.get("/sitemap.xml")
async def sitemap():
    return FileResponse(PUBLIC / "sitemap.xml", media_type="application/xml")


@app.get("/api/health")
async def health():
    return {"ok":True,"ai_configured":bool(API_KEY)}

@app.post("/api/register")
async def register(email:str=Form(...),password:str=Form(...)):
    email=email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): raise HTTPException(400,"Email မှန်ကန်စွာ ထည့်ပါ။")
    if len(password)<8: raise HTTPException(400,"Password အနည်းဆုံး ၈ လုံးထားပါ။")
    if email==ADMIN_EMAIL: raise HTTPException(409,"ဤ email ကို အသုံးပြု၍မရပါ။")
    with db() as con:
        try:
            cur=con.execute("INSERT INTO users(email,password,role,credits,created_at) VALUES(?,?,?,?,?)",
                (email,pw_hash(password),"user",0,datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError: raise HTTPException(409,"ဤ email ဖြင့် အကောင့်ရှိပြီးသားပါ။")
        uid=cur.lastrowid
        user=con.execute("SELECT id,email,role,credits,is_premium FROM users WHERE id=?",(uid,)).fetchone()
    return {"token":token_for(user),"user":dict(user)}

@app.post("/api/login")
async def login(email:str=Form(...),password:str=Form(...)):
    with db() as con:
        user=con.execute("SELECT * FROM users WHERE email=?",(email.strip().lower(),)).fetchone()
    if not user or not pw_check(password,user["password"]): raise HTTPException(401,"Email သို့မဟုတ် Password မှားနေပါသည်။")
    return {"token":token_for(user),"user":{"id":user["id"],"email":user["email"],"role":user["role"],"credits":user["credits"],"is_premium":user["is_premium"]}}

@app.get("/api/me")
async def me(authorization:Optional[str]=Header(None)):
    return {"user":current_user(authorization)}

@app.post("/api/recap")
async def recap(file:UploadFile=File(...),language:str=Form("my"),mode:str=Form("recap"),
                ratio:str=Form("9:16"),fit_mode:str=Form("crop"),
                authorization:Optional[str]=Header(None)):
    user=current_user(authorization)
    allowed={".mp4",".mov",".mkv",".webm"}
    suffix=Path(file.filename or "").suffix.lower()
    if suffix not in allowed: raise HTTPException(415,"MP4, MOV, MKV, WEBM ဖိုင်ကိုသာ တင်ပါ။")
    job=uuid.uuid4().hex; folder=DATA/job; folder.mkdir()
    source=folder/("source_video"+suffix); audio=folder/"audio.mp3"
    output=folder/"formatted.mp4"; srt=folder/"subtitles.srt"; recapfile=folder/"recap.txt"
    size=0
    try:
        with source.open("wb") as out:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                size+=len(chunk)
                if size>MAX_UPLOAD_MB*1024*1024: raise HTTPException(413,f"ဖိုင်အရွယ်အစား {MAX_UPLOAD_MB}MB ထက် မကျော်ရပါ။")
                out.write(chunk)
        check_and_record_daily_use(user["id"])
        with db() as con:
            con.execute("INSERT INTO jobs(id,user_id,filename,status,created_at) VALUES(?,?,?,?,?)",
                (job,user["id"],file.filename,"processing",datetime.now(timezone.utc).isoformat()))
        run_ffmpeg(["-i",str(source),"-vn","-ac","1","-ar","16000","-b:a","64k",str(audio)])
        transcript_data=await transcribe(audio)
        transcript=(transcript_data.get("text") or "").strip()
        if not transcript: raise HTTPException(422,"အသံမှ စာသားမရပါ။")
        target={"my":"Burmese","en":"English","th":"Thai"}.get(language,"Burmese")
        detail="Provide a detailed structured recap with headings and key points." if mode=="pro" else "Provide a concise recap with key bullet points."
        if mode=="subtitle": detail="Provide a brief description of the video topic and key context."
        recap_text=await ai_text(f"You summarize video transcripts in {target}. Be faithful to the transcript. Do not invent facts. {detail}",
            "Summarize this transcript:\n\n"+transcript[:90000])
        recapfile.write_text(recap_text,encoding="utf-8")
        srt.write_text(make_srt(transcript_data.get("segments",[])),encoding="utf-8")
        vf=format_filter(ratio,fit_mode)
        run_ffmpeg(["-i",str(source),"-filter_complex",vf,"-map","[v]","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-b:a","128k","-movflags","+faststart",str(output)],timeout=2400)
        result={"job_id":job,"recap":recap_text,"transcript":transcript[:10000],
            "downloads":{"video":f"/api/download/{job}/video","srt":f"/api/download/{job}/srt","text":f"/api/download/{job}/text"}}
        if mode=="pro":
            highlight,err=await make_highlights(transcript,transcript_data.get("segments",[]),folder,ratio,fit_mode)
            if highlight: result["downloads"]["highlights"]=f"/api/download/{job}/highlights"
            else: result["highlight_note"]=err
        with db() as con: con.execute("UPDATE jobs SET status='done' WHERE id=?",(job,))
        return result
    except Exception:
        with db() as con: con.execute("UPDATE jobs SET status='failed' WHERE id=?",(job,))
        raise
    finally:
        await file.close()

@app.get("/api/download/{job_id}/{kind}")
async def download(job_id:str,kind:str,authorization:Optional[str]=Header(None)):
    user=current_user(authorization)
    if not re.fullmatch(r"[a-f0-9]{32}",job_id): raise HTTPException(400,"Invalid job id")
    with db() as con: job=con.execute("SELECT user_id FROM jobs WHERE id=?",(job_id,)).fetchone()
    if not job or (job["user_id"]!=user["id"] and user["role"]!="admin"): raise HTTPException(404,"ဖိုင်မတွေ့ပါ။")
    folder=DATA/job_id
    options={"video":(folder/"formatted.mp4","formatted-video.mp4"),"srt":(folder/"subtitles.srt","subtitles.srt"),
             "text":(folder/"recap.txt","video-recap.txt"),"highlights":(folder/"highlights.mp4","ai-highlights.mp4")}
    if kind not in options: raise HTTPException(404,"Not found")
    path,name=options[kind]
    if not path.exists(): raise HTTPException(404,"ဖိုင်မတွေ့ပါ။")
    return FileResponse(path,filename=name)

@app.get("/api/admin/overview")
async def admin_overview(authorization:Optional[str]=Header(None)):
    user=current_user(authorization); require_admin(user)
    with db() as con:
        users=con.execute("SELECT id,email,role,credits,is_premium,created_at FROM users ORDER BY id DESC").fetchall()
        jobs=con.execute("SELECT id,user_id,filename,status,created_at FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return {"users":[dict(x) for x in users],"jobs":[dict(x) for x in jobs]}

@app.post("/api/admin/credits")
async def admin_credits(user_id:int=Form(...),amount:int=Form(...),authorization:Optional[str]=Header(None)):
    admin=current_user(authorization); require_admin(admin)
    if amount==0 or abs(amount)>10000: raise HTTPException(400,"Credit ပမာဏ 1 မှ 10000 အတွင်း ဖြစ်ရပါမည်။")
    with db() as con:
        target=con.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone()
        if not target: raise HTTPException(404,"User မတွေ့ပါ။")
        con.execute("UPDATE users SET credits=MAX(0,credits+?) WHERE id=?",(amount,user_id))
        con.execute("INSERT INTO ledger(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
            (user_id,amount,"Admin credit adjustment",datetime.now(timezone.utc).isoformat()))
    return {"ok":True}


@app.post("/api/admin/premium")
async def admin_premium(user_id:int=Form(...), enabled:int=Form(...), authorization:Optional[str]=Header(None)):
    admin=current_user(authorization); require_admin(admin)
    if enabled not in (0,1):
        raise HTTPException(400,"enabled သည် 0 သို့မဟုတ် 1 ဖြစ်ရပါမည်။")
    with db() as con:
        target=con.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone()
        if not target: raise HTTPException(404,"User မတွေ့ပါ။")
        con.execute("UPDATE users SET is_premium=? WHERE id=?",(enabled,user_id))
    return {"ok":True,"is_premium":bool(enabled)}
