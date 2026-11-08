# canwe-voice — free cloned-voice setup (Chatterbox + Modal + Telegram + Google Drive)

Everything below is free: Modal Starter gives $30/month compute credits (resets monthly),
Telegram bots are free, Google Drive gives 15 GB. Idle cost is $0 (scales to zero).

```
 you (Telegram / any script)
        │  text
        ▼
  Modal web endpoint  ──►  Modal GPU worker (T4)  ──►  Chatterbox Multilingual V3 / Turbo
  /v1/audio/speech            your ref voice on a Volume          │
  /telegram/<secret>                                               ▼ mp3
        │◄──────────────────────────────────────────────────────────┘
        ├──► Telegram reply (mp3)
        └──► Google Drive folder (optional)
```

## 0. Record your reference voice (5 min)
- 15–30 seconds, quiet room, phone mic is fine, no music, speak the way you speak in reels
  (Hinglish is fine). Save as `rahul.wav` or `.m4a`/`.mp3`.
- Tip: record one clip in Hindi/Hinglish and one in English (`rahul_en`) — cloning sounds
  best when the reference language matches the script language.

## 1. Modal (10 min)
```bash
pip install modal
modal setup                      # opens browser, creates free Starter account
modal secret create canwe-voice-secrets \
  API_KEY=$(python -c "import secrets;print(secrets.token_hex(24))") \
  TELEGRAM_BOT_TOKEN=PASTE_LATER \
  TELEGRAM_ALLOWED_IDS=PASTE_LATER \
  GDRIVE_FOLDER_ID= \
  GDRIVE_SA_JSON=
modal deploy voice_api.py        # prints:  https://<you>--canwe-voice-web.modal.run
```
First call downloads ~2 GB of weights into the Volume (one time, ~2 min). After that a
60-second voiceover takes ~10–20 s on a T4.

Add your voice and test from the terminal:
```bash
modal run voice_api.py::add_voice --name rahul --path ./rahul.wav
modal run voice_api.py::say --text "Namaste doston, aaj Claude Code ka naya feature dekhte hain." --lang hi
# -> test.mp3
```
Note the API key:  `modal secret list` won't show values; keep the one you generated.

## 2. Telegram bot (5 min)
1. In Telegram open **@BotFather** → `/newbot` → name it (e.g. `canwe voice`) → copy the token.
2. Get your user id: message **@userinfobot** → copy the number.
3. Update the secret and redeploy:
   ```bash
   modal secret create canwe-voice-secrets API_KEY=<same key> TELEGRAM_BOT_TOKEN=<token> \
     TELEGRAM_ALLOWED_IDS=<your id> GDRIVE_FOLDER_ID= GDRIVE_SA_JSON=   # (--force if it exists)
   modal deploy voice_api.py
   ```
4. Register the webhook (one time):
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<you>--canwe-voice-web.modal.run/telegram/<API_KEY>"
   ```
5. Open the bot → `/start` → send a voice note → then send any text → you get an mp3 back.

Bot commands: `/voices`, `/voice <name>`, `/newvoice <name>`, `/lang hi|en`, `/model multi|turbo`,
`/drive on|off`, `/status`.

## 3. Google Drive (10 min, optional)
1. console.cloud.google.com → new project → **APIs & Services → Enable** "Google Drive API".
2. **Credentials → Create → Service account** → Keys → Add key → JSON → download.
3. In Drive create a folder `canwe-voiceovers`, **Share** it with the service-account email
   (`...@...iam.gserviceaccount.com`) as Editor. Copy the folder id from the URL.
4. Put both into the secret (JSON as a single line):
   ```bash
   modal secret create canwe-voice-secrets --force API_KEY=... TELEGRAM_BOT_TOKEN=... \
     TELEGRAM_ALLOWED_IDS=... GDRIVE_FOLDER_ID=<folder id> GDRIVE_SA_JSON="$(cat sa.json | tr -d '\n')"
   modal deploy voice_api.py
   ```
5. `/drive on` in Telegram, or pass `"upload_to_drive": true` in the API call.
Generated files are also kept on the Modal Volume under `/data/out` (`modal volume ls canwe-voice-data out`).

## 4. Use it from anywhere (ElevenLabs replacement)
```bash
curl -X POST https://<you>--canwe-voice-web.modal.run/v1/audio/speech \
  -H "Authorization: Bearer <API_KEY>" -H "Content-Type: application/json" \
  -d '{"input":"Aaj hum baat karenge Claude Code ke baare mein.","voice":"rahul","language":"hi","model":"multi"}' \
  --output vo.mp3
```
Python: `client.py` → `speak(text, out="vo.mp3")`. n8n / Make: HTTP node, same body.
Anything that supports a custom OpenAI TTS base URL also works (`/v1/audio/speech`).

### tech-reel-studio / Remotion
Replace the ElevenLabs call with:
```python
from client import speak
speak(script_text, out="public/vo.mp3", language="hi", model="multi")
```
Keep the ElevenLabs "Shlok" call as `except:` fallback if the Modal endpoint ever errors.

## Tuning
| Goal | Setting |
|---|---|
| Hinglish narration | `model=multi`, `language=hi`, Devanagari or Roman text both work; Devanagari is auto-detected |
| Punchy English with `[laugh]` `[chuckle]` | `model=turbo` (English only) |
| More energy / drama | `exaggeration=0.7`, `cfg_weight=0.3` |
| Ref voice talks fast, output rushed | `cfg_weight=0.3` |
| Accent bleeding across languages | `cfg_weight=0` |

## Cost sanity check
T4 ≈ $0.59/h on Modal → 60 s of voice ≈ 15 s GPU ≈ $0.0025. $30/month ≈ ~10,000 reel
voiceovers. Avoid re-downloading weights (Volume caches them; egress bandwidth burns credits).

## Backups if Modal is down
- Hugging Face Space (5 GPU-min/day free): `pip install gradio_client` then
  `Client("ResembleAI/Chatterbox-Multilingual-TTS").predict(...)`
- Kaggle / Colab: `pip install chatterbox-tts` and run the same `generate()` call from `voice_api.py`.

## Known limits
- Every Chatterbox output carries an inaudible PerTh watermark (detectable, not audible).
- Very long scripts are auto-split at sentence ends (~300 chars per chunk) and stitched.
- Telegram voice notes are OGG/Opus and lower quality than a proper wav recording; for the
  best clone upload a wav via `add_voice` or the `/v1/audio/voices` endpoint.
