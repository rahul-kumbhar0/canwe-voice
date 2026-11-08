"""
canwe-voice — your own cloned voice as a free ElevenLabs-style API.

One Modal app gives you:
  * GPU worker   : Chatterbox Multilingual V3 (Hindi+English+21 more) and Chatterbox-Turbo
  * HTTP API     : OpenAI-compatible  POST /v1/audio/speech  (drop-in for anything that
                   speaks the OpenAI / ElevenLabs TTS shape) + voice management endpoints
  * Telegram bot : send text -> get mp3 ; send a voice note -> becomes your reference voice
  * Google Drive : every generated mp3 is also uploaded to a Drive folder (optional)

Deploy:   modal deploy voice_api.py
Runs on Modal Starter ($30/mo free credits, scales to zero when idle).
"""

import io
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import modal

APP_NAME = "canwe-voice"
DATA = Path("/data")                  # persistent volume
VOICES_DIR = DATA / "voices"          # reference clips  (<name>.wav)
OUT_DIR = DATA / "out"                # generated audio
SETTINGS_FILE = DATA / "settings.json"
HF_CACHE = DATA / "hf"                # model weights cached here (downloaded once)

DEFAULT_VOICE = "rahul"
DEFAULT_LANG = "hi"                   # hi | en | ... (23 langs)
DEFAULT_MODEL = "multi"               # multi (V3, any language) | turbo (English + [laugh] tags)

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(f"{APP_NAME}-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git")
    .pip_install(
        "chatterbox-tts==0.1.7",      # API verified against this version
        "fastapi[standard]",
        "httpx",
        "google-api-python-client",
        "google-auth",
    )
    .env({"HF_HOME": str(HF_CACHE), "HF_HUB_ENABLE_HF_TRANSFER": "0"})
)

# ---- secrets (create once, see README) --------------------------------------
# modal secret create canwe-voice-secrets \
#     API_KEY=<any-long-random-string> \
#     TELEGRAM_BOT_TOKEN=<from @BotFather> \
#     TELEGRAM_ALLOWED_IDS=<your telegram user id> \
#     GDRIVE_FOLDER_ID=<drive folder id or empty> \
#     GDRIVE_SA_JSON='<service account json or empty>'
secrets = [modal.Secret.from_name(f"{APP_NAME}-secrets")]


# =============================================================================
# helpers (used by both GPU worker and web app)
# =============================================================================
def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    volume.commit()


def _chat_cfg(chat_id) -> dict:
    s = _load_settings()
    return s.get(str(chat_id), {"voice": DEFAULT_VOICE, "lang": DEFAULT_LANG, "model": DEFAULT_MODEL})


def _set_chat_cfg(chat_id, **kw):
    s = _load_settings()
    cfg = s.get(str(chat_id), {"voice": DEFAULT_VOICE, "lang": DEFAULT_LANG, "model": DEFAULT_MODEL})
    cfg.update(kw)
    s[str(chat_id)] = cfg
    _save_settings(s)
    return cfg


def _guess_lang(text: str, fallback: str) -> str:
    """Devanagari -> hi, otherwise keep the configured language."""
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    return fallback


def ffmpeg_convert(src_bytes: bytes, src_ext: str, dst_ext: str, extra: list[str] | None = None) -> bytes:
    """Convert audio with ffmpeg (ogg/m4a/mp3 -> wav for references, wav -> mp3 for output).

    Uses temp files and lets ffmpeg auto-detect the input container, because m4a/mp4
    (moov atom at the end) cannot be demuxed from a pipe. `src_ext` is only a filename hint.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in.{src_ext or 'bin'}"
        dst = Path(d) / f"out.{dst_ext}"
        src.write_bytes(src_bytes)
        cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src)] + (extra or []) + [str(dst)]
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode()[-500:])
        return dst.read_bytes()


def list_voices() -> list[str]:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in VOICES_DIR.glob("*.wav"))


def save_voice(name: str, wav_bytes: bytes) -> str:
    """Store a reference clip as 24 kHz mono wav (what Chatterbox likes)."""
    name = re.sub(r"[^a-z0-9_-]", "", name.lower()) or DEFAULT_VOICE
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    clean = ffmpeg_convert(wav_bytes, "wav", "wav", ["-ac", "1", "-ar", "24000"])
    (VOICES_DIR / f"{name}.wav").write_bytes(clean)
    volume.commit()
    return name


# =============================================================================
# GPU worker — loads the models once per container, generates speech
# =============================================================================
@app.cls(
    image=image,
    gpu="T4",                      # cheapest GPU on Modal; both models fit easily
    volumes={str(DATA): volume},
    secrets=secrets,
    scaledown_window=120,          # keep warm 2 min after last call, then $0
    timeout=600,
)
class Voice:
    @modal.enter()
    def load(self):
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        t0 = time.time()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.multi = ChatterboxMultilingualTTS.from_pretrained(device=self.device)
        self.turbo = ChatterboxTurboTTS.from_pretrained(device=self.device)
        volume.commit()            # persist freshly downloaded weights
        print(f"models loaded in {time.time()-t0:.1f}s on {self.device}")

    @modal.method()
    def synth(
        self,
        text: str,
        voice: str = DEFAULT_VOICE,
        lang: str = DEFAULT_LANG,
        model: str = DEFAULT_MODEL,
        exaggeration: float = 0.5,
        cfg_weight: float = 0.5,
        fmt: str = "mp3",
    ) -> dict:
        import soundfile as sf

        volume.reload()
        ref = VOICES_DIR / f"{voice}.wav"
        if not ref.exists():
            raise ValueError(f"voice '{voice}' not found. Available: {list_voices()}")

        # Chatterbox handles ~300 chars per call best; split long scripts on sentence ends
        chunks = _split_text(text)
        wavs = []
        for chunk in chunks:
            if model == "turbo":
                wav = self.turbo.generate(chunk, audio_prompt_path=str(ref))
                sr = self.turbo.sr
            else:
                wav = self.multi.generate(
                    chunk,
                    language_id=lang,
                    audio_prompt_path=str(ref),
                    exaggeration=exaggeration,
                    cfg_weight=cfg_weight,
                )
                sr = self.multi.sr
            wavs.append(wav)

        import torch
        full = torch.cat(wavs, dim=-1)
        buf = io.BytesIO()
        sf.write(buf, full.squeeze(0).detach().cpu().numpy(), sr, format="WAV")
        wav_bytes = buf.getvalue()

        if fmt == "mp3":
            audio = ffmpeg_convert(wav_bytes, "wav", "mp3", ["-b:a", "192k"])
        else:
            audio, fmt = wav_bytes, "wav"

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:6]}.{fmt}"
        (OUT_DIR / fname).write_bytes(audio)
        volume.commit()
        duration = full.shape[-1] / sr
        return {"audio": audio, "filename": fname, "duration": round(duration, 2), "format": fmt}


def _split_text(text: str, max_len: int = 300) -> list[str]:
    parts = re.split(r"(?<=[\.\!\?।])\s+", text.strip())
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 <= max_len:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or [text]


# =============================================================================
# Google Drive upload (service account) — optional
# =============================================================================
def drive_upload(filename: str, data: bytes, mime: str = "audio/mpeg") -> str | None:
    sa_json = os.environ.get("GDRIVE_SA_JSON", "").strip()
    folder = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if not sa_json or not folder:
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    f = svc.files().create(
        body={"name": filename, "parents": [folder]},
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return f.get("webViewLink")


# =============================================================================
# Web app: OpenAI-compatible API + Telegram webhook  (runs on cheap CPU)
# =============================================================================
@app.function(image=image, volumes={str(DATA): volume}, secrets=secrets, timeout=600)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
    from fastapi.responses import Response

    api = FastAPI(title="canwe-voice")

    def auth(authorization: str | None):
        key = os.environ.get("API_KEY", "")
        if key and (authorization or "") not in (f"Bearer {key}", key):
            raise HTTPException(401, "bad api key")

    # ---------- OpenAI-compatible TTS ----------------------------------------
    @api.post("/v1/audio/speech")
    async def speech(request: Request, authorization: str | None = Header(None)):
        """
        Body (OpenAI shape):
          {"input": "text", "voice": "rahul", "model": "multi"|"turbo",
           "response_format": "mp3"|"wav", "language": "hi", "exaggeration": 0.5, "cfg_weight": 0.5}
        Returns raw audio bytes. Same URL shape as api.openai.com/v1/audio/speech.
        """
        auth(authorization)
        body = await request.json()
        text = body.get("input") or body.get("text")
        if not text:
            raise HTTPException(400, "input required")
        volume.reload()
        cfg = {
            "voice": body.get("voice", DEFAULT_VOICE),
            "model": "turbo" if str(body.get("model", DEFAULT_MODEL)).startswith("turbo") else "multi",
            "lang": _guess_lang(text, body.get("language", DEFAULT_LANG)),
            "exaggeration": float(body.get("exaggeration", 0.5)),
            "cfg_weight": float(body.get("cfg_weight", 0.5)),
            "fmt": body.get("response_format", "mp3"),
        }
        try:
            r = await Voice().synth.remote.aio(text, **cfg)
        except ValueError as e:
            raise HTTPException(404, str(e))
        link = drive_upload(r["filename"], r["audio"], "audio/mpeg" if r["format"] == "mp3" else "audio/wav") \
            if body.get("upload_to_drive") else None
        headers = {"X-Duration": str(r["duration"]), "X-Filename": r["filename"]}
        if link:
            headers["X-Drive-Link"] = link
        return Response(r["audio"], media_type="audio/mpeg" if r["format"] == "mp3" else "audio/wav", headers=headers)

    @api.get("/v1/audio/voices")
    async def voices(authorization: str | None = Header(None)):
        auth(authorization)
        volume.reload()
        return {"voices": list_voices(), "default": DEFAULT_VOICE}

    @api.post("/v1/audio/voices")
    async def add_voice(name: str = Form(...), file: UploadFile = File(...), authorization: str | None = Header(None)):
        """Upload a 15–30 s clean recording (wav/mp3/m4a/ogg) to create or replace a voice."""
        auth(authorization)
        raw = await file.read()
        ext = (file.filename or "x.wav").rsplit(".", 1)[-1].lower()
        wav = raw if ext == "wav" else ffmpeg_convert(raw, {"m4a": "mp4", "ogg": "ogg", "oga": "ogg"}.get(ext, ext), "wav")
        saved = save_voice(name, wav)
        return {"ok": True, "voice": saved, "voices": list_voices()}

    @api.get("/health")
    async def health():
        return {"ok": True, "app": APP_NAME}

    # ---------- Telegram webhook ---------------------------------------------
    # Telegram retries a webhook that takes > ~60 s (a GPU cold start does), which would
    # generate the same message twice. So ack immediately and process in a background call.
    @api.post("/telegram/{secret}")
    async def telegram(secret: str, request: Request):
        key = os.environ.get("API_KEY", "")
        if not key or secret != key:
            raise HTTPException(403)
        await tg_worker.spawn.aio(await request.json())
        return {"ok": True}

    return api


# =============================================================================
# Telegram update processing (spawned by the webhook so it can ack instantly)
# =============================================================================
@app.function(image=image, volumes={str(DATA): volume}, secrets=secrets, timeout=600)
async def tg_worker(upd: dict):
    import httpx

    TG = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN','')}"
    TG_FILE = f"https://api.telegram.org/file/bot{os.environ.get('TELEGRAM_BOT_TOKEN','')}"
    ALLOWED = {x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()}

    async def tg(method: str, **kw):
        async with httpx.AsyncClient(timeout=60) as c:
            return (await c.post(f"{TG}/{method}", json=kw)).json()

    async def tg_send_audio(chat_id, audio: bytes, filename: str, caption: str):
        async with httpx.AsyncClient(timeout=120) as c:
            await c.post(
                f"{TG}/sendAudio",
                data={"chat_id": chat_id, "caption": caption, "title": filename},
                files={"audio": (filename, audio, "audio/mpeg")},
            )

    async def tg_download(file_id: str) -> bytes:
        async with httpx.AsyncClient(timeout=60) as c:
            info = (await c.get(f"{TG}/getFile", params={"file_id": file_id})).json()
            path = info["result"]["file_path"]
            return (await c.get(f"{TG_FILE}/{path}")).content

    HELP = (
        "🎙 *canwe-voice* — your cloned voice\n\n"
        "Send me *text* → I reply with an mp3 in your voice.\n"
        "Send a *voice note* (15–30 s, quiet room) → saved as your reference voice.\n\n"
        "Commands:\n"
        "/voices – list voices\n"
        "/voice <name> – switch voice (send a voice note after /newvoice <name> to add one)\n"
        "/newvoice <name> – next voice note is saved under this name\n"
        "/lang hi|en|… – language for the multilingual model\n"
        "/model multi|turbo – multi = any language, turbo = English + [laugh] [chuckle] tags\n"
        "/drive on|off – also upload every mp3 to Google Drive\n"
        "/status – current settings"
    )

    async def handle(upd: dict):
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg["chat"]["id"]
        uid = str(msg.get("from", {}).get("id", ""))
        if ALLOWED and uid not in ALLOWED:
            await tg("sendMessage", chat_id=chat_id, text="Private bot 🙂")
            return {"ok": True}

        volume.reload()
        cfg = _chat_cfg(chat_id)
        text = (msg.get("text") or "").strip()

        # ---- voice note / audio file = new reference voice ----
        media = msg.get("voice") or msg.get("audio") or msg.get("document")
        if media:
            await tg("sendMessage", chat_id=chat_id, text="⏳ saving your voice…")
            raw = await tg_download(media["file_id"])
            mime = media.get("mime_type", "audio/ogg")
            src = "ogg" if "ogg" in mime or "opus" in mime else ("mp3" if "mpeg" in mime else ("mp4" if "mp4" in mime or "m4a" in mime else "wav"))
            wav = ffmpeg_convert(raw, src, "wav")
            name = cfg.get("pending_voice") or cfg["voice"]
            saved = save_voice(name, wav)
            _set_chat_cfg(chat_id, voice=saved, pending_voice=None)
            await tg("sendMessage", chat_id=chat_id,
                     text=f"✅ voice *{saved}* saved and selected. Send me any text to test it.", parse_mode="Markdown")
            return {"ok": True}

        # ---- commands ----
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.lower().split("@")[0]
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                await tg("sendMessage", chat_id=chat_id, text=HELP, parse_mode="Markdown")
            elif cmd == "/voices":
                await tg("sendMessage", chat_id=chat_id, text="Voices: " + (", ".join(list_voices()) or "none yet — send a voice note"))
            elif cmd == "/voice" and arg:
                if arg in list_voices():
                    _set_chat_cfg(chat_id, voice=arg)
                    await tg("sendMessage", chat_id=chat_id, text=f"🎙 using voice {arg}")
                else:
                    await tg("sendMessage", chat_id=chat_id, text=f"no voice '{arg}'. /newvoice {arg} then send a voice note.")
            elif cmd == "/newvoice" and arg:
                _set_chat_cfg(chat_id, pending_voice=arg)
                await tg("sendMessage", chat_id=chat_id, text=f"🎤 now send a 15–30 s voice note; it will be saved as '{arg}'")
            elif cmd == "/lang" and arg:
                _set_chat_cfg(chat_id, lang=arg.lower()[:2])
                await tg("sendMessage", chat_id=chat_id, text=f"🌐 language {arg.lower()[:2]}")
            elif cmd == "/model" and arg:
                m = "turbo" if arg.lower().startswith("t") else "multi"
                _set_chat_cfg(chat_id, model=m)
                await tg("sendMessage", chat_id=chat_id, text=f"🧠 model {m}")
            elif cmd == "/drive" and arg:
                _set_chat_cfg(chat_id, drive=arg.lower() == "on")
                await tg("sendMessage", chat_id=chat_id, text=f"📁 drive upload {'on' if arg.lower()=='on' else 'off'}")
            elif cmd == "/status":
                await tg("sendMessage", chat_id=chat_id, text=json.dumps(_chat_cfg(chat_id), indent=2))
            else:
                await tg("sendMessage", chat_id=chat_id, text=HELP, parse_mode="Markdown")
            return {"ok": True}

        # ---- plain text = generate speech ----
        if not text:
            return {"ok": True}
        if not list_voices():
            await tg("sendMessage", chat_id=chat_id, text="No voice yet — send me a 15–30 s voice note first 🎤")
            return {"ok": True}
        await tg("sendChatAction", chat_id=chat_id, action="record_voice")
        try:
            r = await Voice().synth.remote.aio(
                text, voice=cfg["voice"], lang=_guess_lang(text, cfg["lang"]), model=cfg["model"], fmt="mp3"
            )
        except Exception as e:
            await tg("sendMessage", chat_id=chat_id, text=f"❌ {e}")
            return {"ok": True}
        caption = f"{cfg['voice']} · {cfg['model']} · {r['duration']}s"
        if cfg.get("drive"):
            try:
                link = drive_upload(r["filename"], r["audio"])
                if link:
                    caption += f"\n📁 {link}"
            except Exception as e:
                caption += f"\n(drive upload failed: {e})"
        await tg_send_audio(chat_id, r["audio"], r["filename"], caption)
        return {"ok": True}

    await handle(upd)


# =============================================================================
# one-off helpers:  modal run voice_api.py::add_voice --name rahul --path ./rahul.wav
#                   modal run voice_api.py::say --text "Namaste doston"
# =============================================================================
@app.function(image=image, volumes={str(DATA): volume}, secrets=secrets)
def _store_voice(name: str, data: bytes, ext: str) -> list[str]:
    wav = data if ext == "wav" else ffmpeg_convert(data, {"m4a": "mp4"}.get(ext, ext), "wav")
    save_voice(name, wav)
    return list_voices()


@app.local_entrypoint()
def add_voice(name: str = DEFAULT_VOICE, path: str = "rahul.wav"):
    p = Path(path)
    voices = _store_voice.remote(name, p.read_bytes(), p.suffix.lstrip(".").lower())
    print("voices now:", voices)


@app.local_entrypoint()
def say(text: str = "Namaste doston, yeh meri cloned awaaz hai.", voice: str = DEFAULT_VOICE,
        lang: str = DEFAULT_LANG, model: str = DEFAULT_MODEL, out: str = "test.mp3"):
    r = Voice().synth.remote(text, voice=voice, lang=lang, model=model, fmt="mp3")
    Path(out).write_bytes(r["audio"])
    print(f"wrote {out} ({r['duration']}s)")
