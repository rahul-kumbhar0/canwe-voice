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
DEFAULT_LANG = "en"                   # en | hi | ... (23 langs)
DEFAULT_MODEL = "multi"               # multi (V3, any language) | turbo (English + [laugh] tags)

# GPU is chosen at deploy time. Empty = CPU-only (works on Modal's free tier without a card,
# but each voice-over takes minutes). Set the GitHub repo variable CANWE_GPU=T4 once a payment
# method is on the Modal account (that also unlocks the full $30/mo free credit).
GPU = os.environ.get("CANWE_GPU", "").strip() or None

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
    # hf-xet keeps log files open inside HF_HOME (on the Volume), which breaks
    # volume.commit()/reload(). Remove it; huggingface_hub falls back to plain HTTP.
    .run_commands("pip uninstall -y hf-xet || true")
    .env({
        "HF_HOME": str(HF_CACHE),
        "HF_HUB_ENABLE_HF_TRANSFER": "0",
        # hf-xet keeps log files open under HF_HOME, which makes volume.commit() fail
        # ("open files preventing the operation"). Plain HTTP downloads are fine here.
        "HF_HUB_DISABLE_XET": "1",
        "HF_XET_CACHE": "/tmp/hf-xet",
    })
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
    if s.get("_defaults_v") != 2:          # v2: default language switched hi -> en
        for k, v in s.items():
            if isinstance(v, dict) and v.get("lang") == "hi":
                v["lang"] = DEFAULT_LANG
        s["_defaults_v"] = 2
        _save_settings(s)
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
    # Chatterbox conditions on roughly the first 10 s, so drop leading/trailing silence and
    # normalise loudness: those seconds should be pure, even speech.
    trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.2,areverse,"
            "loudnorm=I=-20:TP=-2")
    clean = ffmpeg_convert(wav_bytes, "wav", "wav", ["-ac", "1", "-af", trim, "-ar", "24000"])
    (VOICES_DIR / f"{name}.wav").write_bytes(clean)
    volume.commit()
    return name


# =============================================================================
# GPU worker — loads the models once per container, generates speech
# =============================================================================
@app.cls(
    image=image,
    gpu=GPU,                       # "T4" when CANWE_GPU is set, else CPU
    cpu=None if GPU else 4.0,
    memory=None if GPU else 8192,
    volumes={str(DATA): volume},
    secrets=secrets,
    scaledown_window=120,          # keep warm 2 min after last call, then $0
    timeout=600 if GPU else 1800,
)
class Voice:
    @modal.enter()
    def load(self):
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        t0 = time.time()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            torch.set_num_threads(4)
        self.multi = ChatterboxMultilingualTTS.from_pretrained(device=self.device)
        self._turbo = None         # loaded on first use (saves RAM/time when only Hindi is used)
        _safe_commit()             # persist freshly downloaded weights
        print(f"multi model loaded in {time.time()-t0:.1f}s on {self.device}")

    @property
    def turbo(self):
        if self._turbo is None:
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            self._turbo = ChatterboxTurboTTS.from_pretrained(device=self.device)
            _safe_commit()
        return self._turbo

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

        _safe_reload()
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
        _safe_commit()
        duration = full.shape[-1] / sr
        return {"audio": audio, "filename": fname, "duration": round(duration, 2), "format": fmt}


def _safe_reload():
    """Reload the volume; an open file inside it must not block generation."""
    try:
        volume.reload()
    except Exception as e:
        print(f"volume.reload() skipped: {e}")


def _safe_commit():
    """Commit the volume; never let a commit failure kill speech generation."""
    try:
        volume.commit()
    except Exception as e:
        print(f"volume.commit() skipped: {e}")


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
@app.function(image=image, volumes={str(DATA): volume}, secrets=secrets, timeout=1800)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
    from fastapi.responses import JSONResponse, Response

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

    # ---------- signed download links for generated files -------------------
    def _sig(name: str) -> str:
        import hashlib
        import hmac
        return hmac.new(os.environ.get("API_KEY", "").encode(), name.encode(), hashlib.sha256).hexdigest()[:32]

    @api.get("/v1/files/{name}")
    async def get_file(name: str, sig: str = ""):
        import hmac
        if not re.fullmatch(r"[0-9a-z_]+\.(mp3|wav)", name) or not hmac.compare_digest(sig, _sig(name)):
            raise HTTPException(404)
        volume.reload()
        f = OUT_DIR / name
        if not f.exists():
            raise HTTPException(404)
        return Response(f.read_bytes(), media_type="audio/mpeg" if name.endswith(".mp3") else "audio/wav",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------- MCP server (Streamable HTTP, JSON responses) -----------------
    # Add in claude.ai -> Settings -> Connectors -> Add custom connector:
    #     https://<workspace>--canwe-voice-web.modal.run/mcp/<API_KEY>
    # Then any Claude chat / skill (e.g. tech-reel-studio) can call generate_voiceover.
    MCP_TOOLS = [
        {
            "name": "generate_voiceover",
            "description": (
                "Generate a voice-over in Rahul's own cloned voice (canwe.ai) and return a download URL "
                "for the mp3. Use this for every reel voice-over instead of ElevenLabs. Default language is "
                "English ('en'); pass language 'hi' only for Hindi/Hinglish scripts. Model 'turbo' is "
                "English-only and supports [laugh]/[chuckle]/[sigh] tags. Download the URL (e.g. curl -L -o "
                "vo.mp3 '<url>') and use the file as the reel's audio track. First call after idle can take "
                "1-2 minutes while the GPU wakes up."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The full script to speak."},
                    "language": {"type": "string", "default": DEFAULT_LANG,
                                 "description": "en (English, default), hi (Hindi/Hinglish), or another Chatterbox language code."},
                    "model": {"type": "string", "enum": ["multi", "turbo"], "default": DEFAULT_MODEL},
                    "voice": {"type": "string", "default": DEFAULT_VOICE},
                    "exaggeration": {"type": "number", "default": 0.5,
                                     "description": "0.25-1.0. Higher = more energy/drama."},
                    "cfg_weight": {"type": "number", "default": 0.5,
                                   "description": "0-1. Lower (0.3) = slower, calmer pacing; 0 removes accent bleed."},
                    "upload_to_drive": {"type": "boolean", "default": False},
                },
                "required": ["text"],
            },
        },
        {
            "name": "list_voices",
            "description": "List the cloned reference voices available for generate_voiceover.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    async def _mcp_call(name: str, args: dict, base: str) -> dict:
        if name == "list_voices":
            volume.reload()
            return {"voices": list_voices(), "default": DEFAULT_VOICE}
        if name != "generate_voiceover":
            raise ValueError(f"unknown tool {name}")
        text = (args.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        r = await Voice().synth.remote.aio(
            text,
            voice=args.get("voice", DEFAULT_VOICE),
            lang=_guess_lang(text, args.get("language", DEFAULT_LANG)),
            model="turbo" if str(args.get("model", DEFAULT_MODEL)).startswith("turbo") else "multi",
            exaggeration=float(args.get("exaggeration", 0.5)),
            cfg_weight=float(args.get("cfg_weight", 0.5)),
            fmt="mp3",
        )
        out = {"url": f"{base}/v1/files/{r['filename']}?sig={_sig(r['filename'])}",
               "filename": r["filename"], "duration_seconds": r["duration"], "format": "mp3"}
        if args.get("upload_to_drive"):
            try:
                out["drive_link"] = drive_upload(r["filename"], r["audio"])
            except Exception as e:
                out["drive_error"] = str(e)
        return out

    @api.get("/mcp/{secret}")
    async def mcp_get(secret: str):
        raise HTTPException(405)          # no server-initiated SSE stream

    @api.post("/mcp/{secret}")
    async def mcp(secret: str, request: Request):
        import hmac
        key = os.environ.get("API_KEY", "")
        if not key or not hmac.compare_digest(secret, key):
            raise HTTPException(403)
        msg = await request.json()
        base = f"https://{request.headers.get('host', '')}"

        async def handle(m: dict):
            mid, method, params = m.get("id"), m.get("method", ""), m.get("params") or {}
            if mid is None:                # notification (e.g. notifications/initialized)
                return None
            def ok(result):
                return {"jsonrpc": "2.0", "id": mid, "result": result}
            if method == "initialize":
                return ok({
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "canwe-voice", "version": "1.0.0"},
                    "instructions": "Rahul's cloned voice. Call generate_voiceover with the reel script; "
                                    "download the returned url and use it as the voice-over track.",
                })
            if method == "ping":
                return ok({})
            if method == "tools/list":
                return ok({"tools": MCP_TOOLS})
            if method == "tools/call":
                try:
                    res = await _mcp_call(params.get("name", ""), params.get("arguments") or {}, base)
                    return ok({"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                               "structuredContent": res, "isError": False})
                except Exception as e:
                    return ok({"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}

        if isinstance(msg, list):
            out = [r for r in [await handle(m) for m in msg] if r is not None]
        else:
            out = await handle(msg)
        if not out:
            return Response(status_code=202)
        return JSONResponse(out)

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
@app.function(image=image, volumes={str(DATA): volume}, secrets=secrets, timeout=1800)
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
        volume.reload()
        if ALLOWED:
            allowed = uid in ALLOWED
        else:
            # No TELEGRAM_ALLOWED_IDS set: the first person to message the bot becomes its owner.
            settings = _load_settings()
            if not settings.get("_owner") and uid:
                settings["_owner"] = uid
                _save_settings(settings)
                await tg("sendMessage", chat_id=chat_id,
                         text=f"👋 You are now the owner of this bot (Telegram id {uid}). Others get 'Private bot'.")
            allowed = uid == settings.get("_owner")
        if not allowed:
            await tg("sendMessage", chat_id=chat_id, text="Private bot 🙂")
            return {"ok": True}

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



@app.function(image=image, secrets=secrets)
def setup_webhook() -> dict:
    """Point @canweaibot at the deployed web endpoint. Runs inside Modal so the bot token
    and API key never leave the Modal secret.  modal run voice_api.py::setup_webhook"""
    import httpx

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.environ.get("API_KEY", "").strip()
    if not token or not key:
        return {"ok": False, "skipped": "TELEGRAM_BOT_TOKEN or API_KEY missing in canwe-voice-secrets"}
    base = modal.Function.from_name(APP_NAME, "web").get_web_url().rstrip("/")
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data={"url": f"{base}/telegram/{key}", "drop_pending_updates": "true"},
        timeout=30,
    ).json()
    info = httpx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=30).json().get("result", {})
    out = {"ok": r.get("ok"), "description": r.get("description"), "endpoint": base,
           "pending_updates": info.get("pending_update_count"), "last_error": info.get("last_error_message")}
    print("webhook:", out)
    if not r.get("ok"):
        raise RuntimeError(f"setWebhook failed: {r.get('description')}")
    return out

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
