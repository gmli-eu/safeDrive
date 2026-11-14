# SafeDrive

Hardware for safer driving.

```
Atech board (ESP32-S3)  --Wi-Fi/MQTT-->  Jetson (WendyOS): Mosquitto + hub.py  --HTTPS-->  Telegram
                                           + voice.py + Ollama (Gemma 4) --HTTPS-->  Gemini, Nemotron (Verda)
                                           ^
iPhone (OwnTracks) ---MQTT location--------+
```

| Folder           | Runs on                            | What it does                                                                                                  |
| ---------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `jetson-hub/`    | Jetson (Wendy app `car-hub`)       | Mosquitto broker plus `hub.py`: turns the SOS long press into Telegram alerts with live location; `voice.py`: the voice assistant |
| `atech-flasher/` | Jetson (Wendy app `atech-flasher`) | Builds the Atech firmware (`project.yaml`), flashes it to the board over USB, then streams the board's output |
| `unoq-camera/`   | Arduino UNO Q (systemd service)    | `stream.py`: serves the USB camera's MJPEG over HTTP on port 8090 (`/stream.mjpg`, `/frame.jpg`, `/health`) |
| `tools/`         | dev machine                        | `deploy.py` deploys an app and waits until it's up; `mqtt_peek.py` watches the broker                         |

## Setup

1. **Secrets** (both are ignored by git):
    - `jetson-hub/secrets.json`: copy `secrets.example.json` and add the bot tokens from BotFather. For "ask Google", add `gemini_api_key` (from aistudio.google.com), or put `GEMINI_API_KEY=...` in the repo's `.env`; without it everything else still works.
    - `atech-flasher/secrets.json`: copy `secrets.example.json` and add the Wi-Fi name and password.
    - `unoq-camera/secrets.json`: copy `secrets.example.json` and add the Uno Q's Linux password and its address on the hotspot.
2. **Wi-Fi**: the board only sees 2.4 GHz. On an iPhone hotspot, turn on _Maximize Compatibility_. The firmware expects the Jetson at `172.20.10.2` (`MQTT_HOST` in `atech-flasher/project.yaml`).
3. **Phone location**: install OwnTracks and set Mode MQTT, Host `172.20.10.2`, Port `1883`, TLS off, Authentication off, location access _Always_, monitoring _Move_. Turn on remote commands (`cmd`) too: when the hub's last location is over 1 minute old, it asks the phone for a new one.
4. **Telegram**: each parent opens their bot and taps **Start**. The bot replies "✅ SafeDrive connected".
5. **Live location through Telegram** (works better than OwnTracks on an iPhone): on the driver's phone, open one of the SafeDrive bots, tap **Start**, then 📎 → Location → **Share My Live Location** → **Until I turn it off**. The hub takes locations only from chats linked to a bot in `jetson-hub/secrets.json`. Telegram only sends a new position when the phone moves, so while sharing is on, the last position counts for up to 6 hours (`LIVE_SHARE_MAX_AGE`). If sharing stops, it counts for 30 minutes like an OwnTracks fix.

## Deploy

```sh
python3 tools/deploy.py jetson-hub      # ~2 s once cached
python3 tools/deploy.py atech-flasher   # ~1 min: build, flash, wait for the board's MQTT connection
python3 tools/deploy.py unoq-camera     # ~5 s: copy stream.py to the Uno Q over SSH, restart, wait for frames
```

`deploy.py` runs the app's `make_compose.py`, which generates `compose.yml` with the code and secrets embedded. Then it runs `wendy run --detach` and follows the logs until the app is up. The device defaults to `wendyos-hackathon-device-4.local`; set `WENDY_DEVICE` to override it. Each run's `compose.yml` is generated and holds secrets, so it isn't committed.

## SOS button (port 6)

- Hold 2 s: two amber flashes, and the alert goes out to every linked chat at once, with a live location. A photo from the car's camera follows, then a clip from 10 s before to 5 s after the press (see Camera). If the last location is over 1 minute old, the hub asks the phone for a new one and the live location moves there when it arrives. With no location at all, the live location follows as soon as the phone answers (up to 60 s).
- Otherwise the light stays off, so the button stays discreet. It never turns red, even when the hub reports a problem (no chats linked, every send failed, or no recent GPS fix). The problem only shows up in the board's log.
- The white light while it's pressed is built into the V2.0 button, and firmware can't turn it off.

## Voice assistant (button on port 5, mic on ports 3-4, speaker on ports 10-11)

- Hold the button while you talk, or tap it once to start and tap again when you're done. It chimes and stays green while it listens. Say what you want, for example:
    - "Hey car, send a message to mom, I'm running 5 min late" (also dad, or both): a Telegram text from that bot, "💬 From the driver: ...". No location is attached.
    - "Hey car, ask Google what's the weather for today", or "find the nearest open pharmacy and text mom I'm stopping there": a Gemini agent with Google Search, Google Maps (at the phone's last location) and the car's own functions (send a message, the light, the cabin camera's latest verdict). It chains them and the answer is read out; it says "One moment" first.
    - "Hey car, turn on the light" / "turn off the light": the light grid on port 7.
    - Anything else, like a quick fact or a joke, gets a short answer from the local model.
- Listening stops when you let go (after holding it for more than 0.6 s) or on the second tap (chime, light off), or after 20 s. If it heard no speech at all, it says so and does nothing.
- Mic audio goes to the Jetson over UDP (port 1885), not MQTT, so a busy network can never freeze the board's loop or the SOS button. A lost packet is a short gap in the audio.
- The screen shows what it heard and the answer, and the speaker reads it out. Pressing the button again interrupts the answer and starts a new question.
- Everything runs on the Jetson except "ask Google": speech-to-text (faster-whisper `small.en`, CPU), the local LLM that decides what to do (Gemma 4 E2B, `gemma4:e2b` on Ollama, GPU, thinking off) and the voice (Piper `en_US-lessac-medium`). While the LLM is still loading, the common phrasings above still work.
- The first start downloads about 8 GB (Ollama image, Gemma, Whisper and Python packages) into the app's volumes. `voice.py` downloads the LLM itself, into Ollama's store, over IPv4: Ollama's own pull uses IPv6, which the phone hotspot keeps resetting. Later starts don't download anything.
- An accident countdown stops listening, silences the speaker and drops the half-finished request.
- Override the models with `llm_model`, `whisper_model`, `piper_voice` or `gemini_model` in `jetson-hub/secrets.json`.

## Cabin watch (NVIDIA Nemotron 3.5 Super VL)

- `voice.py` sends Nemotron 3.5 Super VL (Verda endpoint, `VERDA_INFERENCE_KEY` in `.env` or `verda_inference_key` in `jetson-hub/secrets.json`) a burst of 3 camera frames from the last 1.5 s, downscaled to 640 px, one request at a time, as fast as the endpoint answers (a few seconds when it isn't busy).
- It judges two things. **Danger to the driver**: only a weapon, hitting or restraining the driver, or grabbing the wheel, gear lever or handbrake counts as danger. That goes to `hub.py` (`car/vision/threat`), which alerts every linked chat silently like SOS, with photo and clip; nothing shows or sounds in the car. A blocked view or covered eyes is only logged. **Back-seat hand signs** (volume down/up, slower, faster, thank you, need a break, all good): spoken to the driver ("From the back seat: ...") after two identical answers in a row, never acted on.
- Every verdict is published, retained, on `car/vision/nemotron`; the Gemini agent can read the latest one ("is everything OK in the back?").

## Check-in menu (screen on ports 1-2, knob on ports 13-14)

- Turn the knob to move through the menu and press it to select.
- **I'm ok** opens: Send to mom, Send to dad, Send to both, Go back.
- Picking a recipient sends "I'm ok and I've arrived." plus the phone's location right away, using the last location from up to 30 minutes ago. If it's over 1 minute old, the hub also asks the phone for a new one and sends a second pin if the phone answers from more than 100 m away to that bot (`mom` and `dad` in `jetson-hub/secrets.json`). The screen then shows **Sent** (green), **Sent** without location (orange, no recent GPS fix) or **Failed** with the reason (red), and goes back to the menu. You can press the knob to close it sooner.
- **Go back** returns to the main menu.

## Accident detection (orientation sensor on port 9)

- **Crash**: a jolt of more than 2.5 g in total (`CRASH_G` in `atech-flasher/project.yaml`). The sensor tops out at 2 g per axis, so only a hard knock gets there.
- **Flip**: the board turns more than 75° away from how it sat at power-on (`FLIP_DEG`; a car on its side is about 90°) and stays like that, still, for 3 s. Not while the voice button is in use or for 10 s after (`FLIP_VOICE_GRACE_MS`): picking the board up to talk into it looks just like a roll. Power the board on while it sits in its mounted position: that's the "upright" it compares against.
- Either one shows **POS ACCIDENT DETECTED / ARE YOU OK?** with a 10 s countdown, and the knob ring turns red. Press the knob for **I'm ok** to cancel.
- If nobody presses, every linked chat gets "possible accident detected" with the live location, plus: call the driver, and if they don't answer, call emergency services (`EMERGENCY_NUMBER`, default `112`). The location works the same as for the SOS button. The screen shows **Alert sent**, **Failed** with the reason, or **No reply**.
- If the hub can't be reached when the countdown ends, the alert waits and goes out as soon as it can. Until then, a knob press still cancels it.
- The alert also gets a photo from the car's camera and a clip from 15 s before the impact through the countdown (see Camera).
- Nothing calls emergency services automatically. The message asks the people in the chats to do it.

## Camera (Arduino UNO Q + Arduino USB Camera)

- The camera goes into a powered USB-C hub on the Uno Q's only USB-C port. The Uno Q joins the hotspot by itself and serves the camera at `http://172.20.10.4:8090/` (live view), `/frame.jpg` (newest frame) and `/stream.mjpg`.
- The frames are the camera's own JPEGs, never re-encoded: 1280×720 at 15 fps, about 90 KB each (about 1.3 MB/s). Change `WIDTH`, `HEIGHT` and `FPS` in `unoq-camera/safedrive-camera.service`.
- On the Jetson, `vision.py` (part of `car-hub`) keeps the last 40 s of frames in RAM only. With an SOS or accident alert, `hub.py` sends every linked chat a photo, then an H.264 clip (960 px wide, about 15 KB per second of a still scene). If the camera is off, the alert goes out without them.
- Keep the Uno Q off the Jetson's USB: there it shows up as a second serial port next to the Atech board.
