# ==================================================
# STANDALONE AI TOOLS
# ==================================================

from starlette.background import BackgroundTask

ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".mp4",
    ".mpeg", ".mpga", ".webm", ".ogg", ".flac"
}


@app.post("/api/transcribe")
async def standalone_transcribe(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            415,
            "MP3, WAV, M4A, MP4, WEBM, OGG, FLAC ဖိုင်များကိုသာ အသုံးပြုပါ။"
        )

    folder = DATA / ("transcribe_" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / ("audio" + suffix)

    size = 0

    try:
        with audio.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)

                if size > 100 * 1024 * 1024:
                    raise HTTPException(
                        413,
                        "Audio ဖိုင် 100MB ထက် မကျော်ရပါ။"
                    )

                output.write(chunk)

        if size == 0:
            raise HTTPException(400, "Audio ဖိုင်အလွတ်ဖြစ်နေပါသည်။")

        result = await transcribe_long_audio(audio, folder)

        return {
            "ok": True,
            "text": result.get("text", ""),
            "language": result.get("language", "unknown"),
            "segments": result.get("segments", [])
        }

    finally:
        await file.close()
        shutil.rmtree(folder, ignore_errors=True)


@app.post("/api/translate")
async def standalone_translate(
    text: str = Form(...),
    target_language: str = Form("my"),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    if not text.strip():
        raise HTTPException(400, "ဘာသာပြန်ရန် စာသားထည့်ပါ။")

    if len(text) > 30000:
        raise HTTPException(413, "စာသားသည် 30000 characters ထက် မကျော်ရပါ။")

    languages = {
        "my": "natural Burmese",
        "en": "natural English",
        "th": "natural Thai"
    }

    if target_language not in languages:
        raise HTTPException(400, "ဘာသာစကား မမှန်ပါ။")

    translated = await ai_text(
        (
            "You are a professional translator. "
            f"Translate the supplied text into {languages[target_language]}. "
            "Preserve the original meaning, names, numbers and formatting. "
            "Do not summarize or add explanations. "
            "Return only the translated text."
        ),
        text
    )

    return {
        "ok": True,
        "source_text": text,
        "translated_text": translated.strip(),
        "target_language": target_language
    }


@app.post("/api/tts")
async def standalone_tts(
    text: str = Form(...),
    voice: str = Form(TTS_VOICE),
    authorization: Optional[str] = Header(None)
):
    user = current_user(authorization)

    if not text.strip():
        raise HTTPException(400, "အသံထုတ်ရန် စာသားထည့်ပါ။")

    if len(text) > 20000:
        raise HTTPException(
            413,
            "စာသားသည် 20000 characters ထက် မကျော်ရပါ။"
        )

    if voice not in ALLOWED_TTS_VOICES:
        raise HTTPException(400, "Voice အမျိုးအစား မမှန်ပါ။")

    folder = DATA / ("tts_" + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=True)

    try:
        audio = await generate_speech(text, folder, voice)

        if not audio.is_file() or audio.stat().st_size == 0:
            raise HTTPException(500, "AI Voice ဖိုင် မရရှိပါ။")

        return FileResponse(
            audio,
            media_type="audio/mpeg",
            filename="deepclip-ai-voice.mp3",
            background=BackgroundTask(
                shutil.rmtree,
                folder,
                ignore_errors=True
            )
        )

    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise