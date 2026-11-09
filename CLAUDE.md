# CLAUDE.md — canwe-voice project context

> Read this fully before doing anything. It is the hand-off from a claude.ai chat session
> (8 Nov 2026) where the code was written and part of the setup was done. Your job in
> Claude Code is to finish the deployment, verify it end to end, then wire it into
> tech-reel-studio. Work step by step, run the verification after each step, and ask
> Rahul only for things that genuinely need his login or his voice.

## 1. Goal (one paragraph)

Rahul (GitHub `rahul-kumbhar0`) runs the Instagram tech/AI reels page **canwe.ai** and makes
reels with a Claude skill called **tech-reel-studio** (Remotion-based). He currently pays
ElevenLabs for voice-over ("Shlok" Indian voice). He wants a **100% free** replacement that
speaks in **his own cloned voice**, works for **Hinglish/Hindi and English**, and is callable
**from anywhere** like ElevenLabs (HTTP API), with a **Telegram bot** front-end and
**Google Drive** storage. This repo is that system.

## 2. Architecture (decided, do not re-litigate)

```
Telegram (@canweaibot)  ──┐
tech-reel-studio / curl ──┼──► Modal web endpoint (CPU, FastAPI)
                          │      POST /v1/audio/speech   (OpenAI-compatible)
                          │      GET/POST /v1/audio/voices
                          │      POST /telegram/<API_KEY> (webhook)
                          │              │
                          │              ▼
                          │     Modal GPU class `Voice` (T4, scaledown 120 s)
                          │       Chatterbox Multilingual V3  (model="multi", 23 langs incl. hi)
                          │       Chatterbox-Turbo            (model="turbo", English + [laugh] tags)
                          │       reference voices on Modal Volume `canwe-voice-data` (/data/voices/*.wav)
                          │       outputs kept at /data/out/*.mp3
                          └──► optional: Google Drive upload (service account) → folder canwe-voiceovers
```

Why these choices:
- **Chatterbox** (Resemble AI, MIT): zero-shot cloning from a 10–30 s clip, Hindi supported,
  beat ElevenLabs Turbo v2.5 in a blind test. Rahul chose it earlier for tech-reel-studio.
- **Modal Starter**: $30/month free compute credits, resets monthly, scales to zero.
  A 60 s voiceover ≈ 15 s T4 ≈ $0.003. Realistic usage $2–4/month → free in practice.
- **GitHub Actions** deploys on push so no local terminal is needed (`.github/workflows/deploy.yml`).
- **Telegram** = free UI: text → mp3; voice note → becomes the reference voice.
- **Google Drive** = free 15 GB storage. Folder already created:
  `canwe-voiceovers` id `1OqqvX8winexjNInEyoC_qeHhX0uU0ooS`
  (https://drive.google.com/drive/folders/1OqqvX8winexjNInEyoC_qeHhX0uU0ooS).

## 3. Files in this repo

| File | What it is |
|---|---|
| `voice_api.py` | The whole Modal app: image, Volume, GPU class `Voice`, FastAPI web app, Telegram webhook, Drive upload, local entrypoints `add_voice` and `say`. |
| `client.py` | Python client: `speak(text, out, voice, model, language, ...)`, `list_voices()`, `add_voice(name, path)`. Reads `VOICE_API_URL` and `VOICE_API_KEY` env vars. |
| `.github/workflows/deploy.yml` | Runs `modal deploy voice_api.py` on push to `main` (and manual `workflow_dispatch`). Needs repo secrets `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`. |
| `README.md` | Human setup guide (Modal, Telegram, Drive, tuning, cost). |
| `.gitignore` | ignores audio files, `sa.json`, `.env`, `__pycache__`. |
| `requirements.txt` | `modal`, `requests` (local only; the Modal image installs its own deps). |

Key constants in `voice_api.py`: `APP_NAME="canwe-voice"`, `DEFAULT_VOICE="rahul"`,
`DEFAULT_LANG="hi"`, `DEFAULT_MODEL="multi"`, Modal secret name `canwe-voice-secrets`,
Volume name `canwe-voice-data`, HF cache at `/data/hf` (weights downloaded once, ~2 GB).

## 4. Current status (as of hand-off)

| Item | Status | Notes |
|---|---|---|
| Code written & syntax-checked | ✅ | not yet run on Modal — expect small runtime fixes (see §7) |
| GitHub repo `rahul-kumbhar0/canwe-voice` | ✅ public, **empty** | claude.ai connector could not push (read-only). Rahul will add the files from Claude Code. |
| Modal account | ✅ | Starter plan, signed up with GitHub |
| Modal API token `canwe-voice-ci` | ✅ created | ID + secret are in Rahul's chat history / clipboard, NOT in this repo |
| GitHub secret `MODAL_TOKEN_ID` | ✅ added | |
| GitHub secret `MODAL_TOKEN_SECRET` | ⏳ | may still be missing — verify |
| Telegram bot `@canweaibot` | ✅ created | token in Rahul's chat history, NOT in this repo |
| Telegram user id | ⏳ | get from @userinfobot |
| Modal secret `canwe-voice-secrets` | ⏳ | not created yet (see §5 step 2) |
| First deploy | ⏳ | |
| Reference voice uploaded | ⏳ | needs Rahul's 15–30 s recording |
| Telegram webhook registered | ⏳ | |
| Drive service-account JSON | ⏳ optional | can be skipped for now |
| tech-reel-studio integration | ⏳ | after everything above works |

**Secrets are never committed.** This repo is public. All tokens live only in Modal secrets,
GitHub Actions secrets, or Rahul's password manager. If you ever see a token in a file,
remove it and tell Rahul to rotate it. After the system works, Rahul should rotate the
Modal token (modal.com/settings/tokens) and the bot token (`/revoke` in @BotFather) because
both were pasted in chat windows during setup.

## 5. What to do next, in order

### Step 0 — get the files into the repo
If the working directory does not already contain `voice_api.py`, Rahul has `canwe-voice.zip`
(from the chat). Unzip into the repo root, then:
```bash
git add . && git commit -m "canwe-voice initial" && git push -u origin main
```

### Step 1 — confirm GitHub Actions secrets
Ask Rahul to confirm both exist at
https://github.com/rahul-kumbhar0/canwe-voice/settings/secrets/actions :
`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`.
(If `modal` CLI is authenticated locally instead, you can skip Actions and run
`modal deploy voice_api.py` directly.)

### Step 2 — create the Modal secret (Rahul does this in the browser, or via CLI)
https://modal.com/secrets → Create → Custom → name **`canwe-voice-secrets`** with keys:

| Key | Value |
|---|---|
| `API_KEY` | a long random string (generate: `python -c "import secrets;print('cv_'+secrets.token_hex(24))"`) |
| `TELEGRAM_BOT_TOKEN` | the @canweaibot token from BotFather |
| `TELEGRAM_ALLOWED_IDS` | Rahul's numeric Telegram user id (comma-separate if more) |
| `GDRIVE_FOLDER_ID` | `1OqqvX8winexjNInEyoC_qeHhX0uU0ooS` |
| `GDRIVE_SA_JSON` | empty for now |

CLI equivalent:
```bash
modal secret create canwe-voice-secrets API_KEY=... TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_ALLOWED_IDS=... GDRIVE_FOLDER_ID=1OqqvX8winexjNInEyoC_qeHhX0uU0ooS GDRIVE_SA_JSON=
```

### Step 3 — deploy
Push to `main` or run the workflow from the Actions tab, or locally `modal deploy voice_api.py`.
The log prints the endpoint, shape: `https://<workspace>--canwe-voice-web.modal.run`.
Record it in §8 below.

Verify: `curl https://<URL>/health` → `{"ok":true,"app":"canwe-voice"}`.

### Step 4 — add Rahul's reference voice
Preferred (best quality): a clean 15–30 s wav/m4a recording, quiet room, Hinglish, no music.
```bash
modal run voice_api.py::add_voice --name rahul --path ./rahul.wav
# or via API:
curl -X POST https://<URL>/v1/audio/voices -H "Authorization: Bearer <API_KEY>" \
  -F name=rahul -F file=@rahul.wav
```
Fallback: he sends a voice note to @canweaibot after Step 5 (Opus-compressed, slightly lower quality).
Optionally also an English clip as `rahul_en`.

### Step 5 — register the Telegram webhook (one time)
Open in a browser or curl:
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<URL>/telegram/<API_KEY>
```
Expect `{"ok":true,"result":true,"description":"Webhook was set"}`.
Check: `https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo`.

### Step 6 — end-to-end test
1. `modal run voice_api.py::say --text "Namaste doston, aaj Claude Code ka naya feature dekhte hain." --lang hi`
   → `test.mp3`, ask Rahul to listen and rate: robotic? accent off? too fast?
2. Telegram: `/start`, then send a sentence → mp3 reply.
3. API:
```bash
curl -X POST https://<URL>/v1/audio/speech -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"input":"Aaj hum baat karenge Claude Code ke baare mein.","voice":"rahul","language":"hi","model":"multi"}' \
  --output vo.mp3
```
Tune per feedback (§6).

### Step 7 — integrate into tech-reel-studio
- Replace the ElevenLabs TTS call with `client.speak(script_text, out=..., language="hi", model="multi")`.
- Keep ElevenLabs "Shlok" as the `except:` fallback when the Modal endpoint errors.
- Env vars the skill needs: `VOICE_API_URL`, `VOICE_API_KEY`.
- For English hooks with laughs use `model="turbo"` and tags like `[chuckle]`.

### Step 7b — MCP connector (how Claude uses the voice while making reels)
The web app also serves an MCP server at `https://<URL>/mcp/<API_KEY>` (Streamable HTTP, JSON).
Add it in claude.ai → Settings → Connectors → Add custom connector (name: canwe-voice).
Tools: `generate_voiceover(text, language="hi", model="multi", voice, exaggeration, cfg_weight,
upload_to_drive)` → `{url, duration_seconds, filename}` (url = HMAC-signed `/v1/files/<name>?sig=`),
and `list_voices()`. tech-reel-studio: call `generate_voiceover` with the script, `curl -L -o vo.mp3 <url>`,
use as the voice track; fall back to ElevenLabs "Shlok" if the tool errors.

### Step 8 — optional: Google Drive upload
console.cloud.google.com → project → enable Drive API → service account → JSON key → share the
Drive folder `canwe-voiceovers` with the service-account email as Editor → put the JSON
(single line) into `GDRIVE_SA_JSON` in the Modal secret → redeploy → `/drive on` in Telegram
or `"upload_to_drive": true` in API calls.

## 6. Tuning cheat-sheet (Chatterbox)

| Symptom / goal | Change |
|---|---|
| Hinglish in Roman script sounds too English | `language="hi"` with `model="multi"`; or write key words in Devanagari (auto-detected → hi) |
| Accent bleeds from reference language | `cfg_weight=0` |
| Output rushed / reference talks fast | `cfg_weight=0.3` |
| More energy / drama | `exaggeration=0.7`, `cfg_weight=0.3` |
| Flat narration | `exaggeration=0.6` |
| Hallucinated extra words | shorter chunks: lower `max_len` in `_split_text` (300 → 200) |
| Need `[laugh]` `[chuckle]` `[sigh]` | `model="turbo"` (English only) |

## 7. Known risks / likely first-run fixes

- `chatterbox-tts` API: **verified against 0.1.7 (pinned in the image)**. In 0.1.7
  `ChatterboxMultilingualTTS.from_pretrained(device)` takes NO `t3_model` arg (the hand-off code
  passed `t3_model="v3"`, which would have crashed — removed). `generate(text, language_id, audio_prompt_path,
  exaggeration, cfg_weight)` and `.sr` match. Turbo's reference clip must be > 5 s.
- Telegram processing runs in a spawned `tg_worker` function so the webhook acks instantly
  (Telegram retries after ~60 s, which a GPU cold start exceeds → duplicate mp3s otherwise).
- ffmpeg conversions go through temp files with input auto-detect (m4a can't be read from a pipe).
- Torch/CUDA wheel: if the image build fails, add `.pip_install("torch", "torchaudio",
  index_url="https://download.pytorch.org/whl/cu121")` before `chatterbox-tts`.
- T4 has 16 GB; both models fit. If OOM, load only `multi` and lazy-load `turbo`.
- First cold start downloads ~2 GB from Hugging Face into the Volume; later starts ~30–60 s.
- `volume.commit()` is called after writes; `volume.reload()` before reads — keep that pattern.
- Modal free plan: 8 web endpoints max, 1-day log retention. Egress bandwidth costs credits —
  do not re-download weights each run (cached on Volume).
- Telegram webhook URL embeds `API_KEY` as the path secret; the handler also checks
  `TELEGRAM_ALLOWED_IDS` so strangers get "Private bot".
- Every Chatterbox output carries an inaudible PerTh watermark — accepted.
- Weights licence: Chatterbox is MIT (commercial OK). Do not swap to F5-TTS/Fish Speech
  without checking their non-commercial licences.

## 8. Fill in as you go

```
MODAL_URL      = https://rahul-kumbhar0--canwe-voice-web.us-east.modal.run   (deployed 2026-11-09, CPU mode)
API_KEY        = GitHub secret VOICE_API_KEY → synced into Modal secret canwe-voice-secrets; never write it here
GPU            = none (no card on Modal yet). Add card → repo variable CANWE_GPU=T4 → redeploy
Voices on file = rahul, rahul_en (?)
Webhook set    = yes (auto, by deploy workflow)
Test mp3 OK    = yes. Reference = Rahul's own ElevenLabs clone export (42 s). English default.
                 A/B/C test 2026-11-09: C (multi, en, exaggeration 0.4, cfg_weight 0.8) closest → now the default
tech-reel-studio switched = yes / no
```

## 9. Rahul's preferences for this work

- Wants it fully free; no paid tools.
- Prefers that you do things rather than explain them; ask only when his login/voice is needed.
- Hinglish is the default narration language; English for some hooks.
- Keep ElevenLabs "Shlok" as fallback until the clone is clearly good.
- Don't print secrets into files or logs.
