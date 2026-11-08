# Pocket Jarvis

Termux par chalne wala private voice assistant. Wake word bolo ("Jarvis ...") aur phone khud kaam karta hai: call, SMS, torch, photo, volume, battery, location, apps kholna, aur bhi.
**Browser band karne par bhi chalta rehta hai**, kyunki mic aur voice loop Python daemon mein hai. Web page sirf remote control aur transcript hai.

## Setup (ek baar)

1. F-Droid se **Termux**, **Termux:API** (aur auto-start ke liye **Termux:Boot**) install karo. Teeno same source se hone chahiye.
2. Termux mein project folder ke andar: `bash install.sh`
3. Android Settings > Apps > **Termux:API** > Permissions: **Microphone, Camera, Phone, SMS, Contacts, Location** allow karo.
4. Settings > Apps > **Termux** > Battery > **Unrestricted** (warna Android background mein kill kar deta hai).
5. (App kholne ke liye) Termux ko "Display over other apps" permission do.
6. `bash start.sh` aur phir `http://localhost:8000` kholo. Band karne ke liye `bash stop.sh`.

## Kaise use karein

Bolo: **"Jarvis, torch on karo"**. Reply ke baad 25 sec tak bina naam liye follow-up bol sakte ho. "Jarvis, go to sleep" bolne par wapas wake word chahiye hoga.

| Kaam | Example |
|---|---|
| Call | "Jarvis, call papa" / "mummy ko call karo" (pehle "yes" bolna padta hai) |
| SMS | "send sms to papa: main late hoon" / "papa ko message bhejo ki ..." |
| Torch | "torch on", "flashlight band karo" |
| Battery, time, date | "battery kitna hai", "what time is it", "aaj kya tarikh hai" |
| Volume, brightness | "volume 8", "volume up", "mute", "brightness 60" |
| Photo | "take photo", "take selfie" |
| Clipboard | "read clipboard", "copy hello world" |
| Wi-Fi, location | "wifi status", "where am i" |
| Apps | "open youtube", "whatsapp kholo", "open google.com" |
| Music | "play music /sdcard/song.mp3", "pause music" |
| Baaki sawal | Local LLM chalu ho to open-ended jawab deta hai |

## Tuning (`.env`)
- Noise se trigger ho raha hai? `SPEECH_DB=-32`. Aapki awaaz miss ho rahi hai? `SPEECH_DB=-42`.
- Bina Chariot key ke Android ka built-in speech-to-text/TTS use hota hai (`STT_BACKEND=android`); kuch phones par har sunne se pehle beep aata hai.
- Chariot voice badalne ke liye `CHARIOT_VOICE_ID`.

## Files
`server.py` entry point (daemon + web UI) · `listener.py` mic loop, wake word · `brain.py` intents + LLM · `device.py` Termux:API · `voice_engine.py` TTS · `asr_engine.py` STT · `static/` UI.

## Security
- Server sirf `localhost` par sunta hai aur doosri websites ke requests reject karta hai (warna koi bhi site "call" bhej sakti thi).
- **Chariot API key `.env` mein rakho, code mein nahi.** Purani key chat/files mein share ho chuki hai, Chariot dashboard se use revoke karke nayi banao.
