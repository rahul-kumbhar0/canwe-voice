"""
Drop-in client for canwe-voice. Use this anywhere you used ElevenLabs before.

    from client import speak
    speak("Aaj hum baat karenge Claude Code ke baare mein.", out="vo.mp3")

Env vars:  VOICE_API_URL  (https://<you>--canwe-voice-web.modal.run)
           VOICE_API_KEY  (same API_KEY you put in the Modal secret)
"""
import os
import requests

BASE = os.environ.get("VOICE_API_URL", "").rstrip("/")
KEY = os.environ.get("VOICE_API_KEY", "")


def speak(text: str, out: str = "voiceover.mp3", voice: str = "rahul", model: str = "multi",
          language: str = "hi", exaggeration: float = 0.5, cfg_weight: float = 0.5,
          upload_to_drive: bool = False) -> str:
    r = requests.post(
        f"{BASE}/v1/audio/speech",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"input": text, "voice": voice, "model": model, "language": language,
              "exaggeration": exaggeration, "cfg_weight": cfg_weight,
              "response_format": "mp3", "upload_to_drive": upload_to_drive},
        timeout=600,
    )
    r.raise_for_status()
    with open(out, "wb") as f:
        f.write(r.content)
    link = r.headers.get("X-Drive-Link")
    print(f"✓ {out}  {r.headers.get('X-Duration')}s" + (f"  drive: {link}" if link else ""))
    return out


def list_voices():
    r = requests.get(f"{BASE}/v1/audio/voices", headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
    return r.json()


def add_voice(name: str, path: str):
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/v1/audio/voices", headers={"Authorization": f"Bearer {KEY}"},
                          data={"name": name}, files={"file": (os.path.basename(path), f)}, timeout=120)
    return r.json()


if __name__ == "__main__":
    import sys
    speak(" ".join(sys.argv[1:]) or "Namaste doston, yeh meri cloned awaaz hai.")
