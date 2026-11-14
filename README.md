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
| `jetson-hub/`    | Jetson (Wendy app `car-hub`)       | Mosquitto broker plus `hub.py`: turns the SOS long press into Telegram alerts with live location; `voice.py`: the voice assistant; `dash.py`: the SafeDrive page for the driver's phone |
| `atech-flasher/` | Jetson (Wendy app `atech-flasher`) | Builds the Atech firmware (`project.yaml`), flashes it to the board over USB, then streams the board's output |
| `unoq-camera/`   | Arduino UNO Q (systemd service)    | `stream.py`: serves the USB camera's MJPEG over HTTP on port 8090 (`/stream.mjpg`, `/frame.jpg`, `/health`) |
| `tools/`         | dev machine                        | `deploy.py` deploys an app and waits until it's up; `mqtt_peek.py` watches the broker                         |

## Setup

1. **Secrets** (both are ignored by git):
    - `jetson-hub/secrets.json`: copy `secrets.example.json` and add the bot tokens from BotFather. For "ask Google" and the AI incident report, add `gemini_api_key` (from aistudio.google.com), or put `GEMINI_API_KEY=...` in the repo's `.env`; without it everything else still works.
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

`deploy.py jetson-hub` first checks the hub log and refuses to deploy while an SOS, accident or threat alert from the last 2 minutes hasn't finished sending its clip and incident report, because a restart would drop them. `--check` runs only that check; `--force` deploys anyway.

`deploy.py` runs the app's `make_compose.py`, which generates `compose.yml` with the code and secrets embedded. Then it runs `wendy run --detach` and follows the logs until the app is up. The device defaults to `wendyos-hackathon-device-4.local`; set `WENDY_DEVICE` to override it. Each run's `compose.yml` is generated and holds secrets, so it isn't committed.

## SOS button (port 6)

- Hold 2 s: two amber flashes, and the alert goes out to every linked chat at once, with a live location. A photo from the car's camera follows, then a clip from 10 s before to 5 s after the press (see Camera). If the last location is over 1 minute old, the hub asks the phone for a new one and the live location moves there when it arrives. With no location at all, the live location follows as soon as the phone answers (up to 60 s).
- If the phone hasn't sent a position for over 30 minutes (live location sharing stopped, OwnTracks asleep), SOS, accident and threat alerts still send the last known one, up to 24 h old (`LAST_KNOWN_MAX_AGE`), marked "⚠️ Last known position, 45 min ago: the car may have moved". The incident report says the same. The live location moves as soon as the phone answers. Check-ins still only use positions up to 30 minutes old.
- Otherwise the light stays off, so the button stays discreet. It never turns red, even when the hub reports a problem (no chats linked, every send failed, or no recent GPS fix). The problem only shows up in the board's log.
- The white light while it's pressed is built into the V2.0 button, and firmware can't turn it off.

## Voice assistant (button on port 5, mic on ports 3-4, speaker on ports 10-11)

- Hold the button while you talk, or tap it once to start and tap again when you're done. It chimes and stays green while it listens. The button is the wake word: "hey car" is optional. Say what you want, for example:
    - "Hey car, send a message to mom, I'm running 5 min late" (also dad, or both): a Telegram text from that bot, "💬 From the driver: ...". No location is attached.
      A message goes to everyone only if you say "both", "everyone", "all", "family" or "parents" or name every contact; if the model can't tell who, the car asks "Who should I send it to?" instead of guessing, and for 30 s your next press can be just the name ("Dad", "Send it to mom", "Both"; in that answer, sound-alikes such as "that" or "dead" count as dad and "mum" or "ma" as mom). Speech recognition also gets the contact names as hints. Tip: wait for the chime before speaking, and let go half a second after you finish. Common mishearings are fixed first ("send dead" → dad, "mum" → mom).
    - "Hey car, ask Google what's the weather for today", or "find the nearest open pharmacy and text mom I'm stopping there": a Gemini agent with Google Search, Google Maps (at the phone's last location) and the car's own functions (send a message, the light, the cabin camera's latest verdict). It chains them and the answer is read out; it says "One moment" first.
    - "Hey car, directions to Kamppi" (also "take me to ...", "navigate to ..."): a **Go to Kamppi** button on the SafeDrive page on the phone (see SafeDrive page). Without "Google", no AI service sees the destination: Google Maps looks it up when you tap Go. With "Google" ("ask Google for directions to the nearest open pharmacy"), Gemini finds the exact place with Maps first and puts that on the button.
    - "Hey car, turn on the light" / "turn off the light": the light grid on port 7.
    - Anything else, like a quick fact or a joke, gets a short answer from the local model.
- Listening stops when you let go (after holding it for more than 0.6 s) or on the second tap (chime, light off), or after 20 s. If it heard no speech at all, it says so and does nothing.
- Mic audio goes to the Jetson over UDP (port 1885), not MQTT, so a busy network can never freeze the board's loop or the SOS button. A lost packet is a short gap in the audio.
- The screen shows what it heard and the answer, and the speaker reads it out. Pressing the button again interrupts the answer and starts a new question.
- Nothing you say goes online unless you say "Google" (or "Gemini"): then exactly what you said goes to Gemini, with the car's location. Anything else is handled in the car; "what's the weather?" without "Google" gets "I can't check that offline. Say 'ask Google' to look it up." Messages to contacts still need the internet for Telegram, but no AI service sees them.
- Everything runs on the Jetson except "ask Google": speech-to-text (faster-whisper `small.en`, CPU), the local LLM that decides what to do (Gemma 4 E2B, `gemma4:e2b` on Ollama, GPU, thinking off) and the voice (Piper `en_US-lessac-medium`). While the LLM is still loading, the common phrasings above still work.
- The first start downloads about 8 GB (Ollama image, Gemma, Whisper and Python packages) into the app's volumes. `voice.py` downloads the LLM itself, into Ollama's store, over IPv4: Ollama's own pull uses IPv6, which the phone hotspot keeps resetting. Later starts don't download anything.
- An accident countdown stops listening, silences the speaker and drops the half-finished request.
- Override the models with `llm_model`, `whisper_model`, `piper_voice` or `gemini_model` in `jetson-hub/secrets.json`.
- Spoken answers are made louder in software: each sentence is softly compressed and its peaks set to 75 % of full scale, because the board's speaker volume can't go above 0.6 without clipping, and full-scale speech broke up. In `jetson-hub/secrets.json`, `voice_peak` sets the peak level (default 0.75; lower it if the sound breaks up) and `voice_drive` the compression (default 2.0, 0 = off, 3 = louder but harsher).

## Cabin watch (NVIDIA Nemotron 3.5 Super VL)

- `voice.py` sends Nemotron 3.5 Super VL (Verda endpoint, `VERDA_INFERENCE_KEY` in `.env` or `verda_inference_key` in `jetson-hub/secrets.json`) a burst of 3 camera frames from the last 1.5 s, downscaled to 640 px, one request at a time, as fast as the endpoint answers (a few seconds when it isn't busy).
- It judges two things. **Danger to the driver**: only a weapon, hitting or restraining the driver, or grabbing the wheel, gear lever or handbrake counts as danger. That goes to `hub.py` (`car/vision/threat`), which alerts every linked chat silently like SOS, with photo and clip; nothing shows or sounds in the car. A blocked view or covered eyes is only logged. **Hand signs** (see below): spoken to the driver once Nemotron reports the same sign in 2 of its last 3 answers, never acted on.
- Every verdict is published, retained, on `car/vision/nemotron`; the Gemini agent can read the latest one ("is everything OK in the back?").
- If the camera can't see (the picture is almost uniformly black: lid closed, lens blocked, or a dark cabin at night, since the camera has no infrared) or sends nothing, Nemotron isn't asked, and the board's screen shows a crossed-out camera icon in the top-right corner until the picture is back (`car/vision/camera`, retained: `ok`, `dark` or `off`).

### Hand signs to try

Make the sign toward the camera and hold it still for 2-3 seconds: the car speaks it once Nemotron reports it in 2 of its last 3 answers (about one a second), and not again until it's gone from the last 3.

| Sign | How to make it | The car says |
|---|---|---|
| 👍 All good | Thumbs up | "Hand sign: All good back here." |
| ⏸️ Need a break | Both hands make a T (time-out) in front of you: one flat hand horizontal, the other upright and touching it | "Hand sign: Can we stop for a break soon?" |
| 🔉 Volume down | Flat hand, palm down, push downward | "Hand sign: Could you turn the volume down, please?" |
| 🔊 Volume up | Flat hand, palm up, lift upward | "Hand sign: Could you turn the volume up, please?" |
| ✋ Slower | Flat hand, palm toward the camera, fingers up, held still like a stop sign | "Hand sign: Could you slow down, please?" |
| 🔄 Faster | Roll your hand forward in circles, or circle a finger | "Hand sign: Could you go a bit faster?" |
| 🙏 Thank you | Flat hand, fingers together, fingertips at your chin, then move it forward and down | "Hand sign: Thank you!" |

- Waving, talking with your hands, touching your hair, glasses, ears or nose and resting your chin on a curled hand don't count. Slow, held signs work best: fast movement blurs on the camera.
- Anyone the camera sees can sign, the driver included (`gesture_from: "any"`, the default, for demos at a table). In a real car, set `"gesture_from": "back"` in `jetson-hub/secrets.json`: then only people in the back seat count, and the car says "From the back seat: ..." instead of "Hand sign: ...".
- The car stays quiet while you're talking to it. The hub log shows what Nemotron saw for each verdict, including a `hands=` description of the hands.

## SafeDrive page (phone in the holder)

- On the driver's phone, open `http://172.20.10.2:8092/` in Safari (the phone is the hotspot, so it always reaches the Jetson) and leave the page on screen. Add it to the home screen to open it faster.
- Set **Settings → Display & Brightness → Auto-Lock → Never** on that phone. The page can't keep the screen on by itself: the Wake Lock API only works on HTTPS pages, and a silent-video workaround doesn't work on current iOS.
- Ask the car for directions and the page shows the place with a big **Go to ... →** button. One tap opens Google Maps with the route, ready to start. **Open in Waze instead** does the same in Waze. Both open the website if the app isn't installed.
- Nothing opens by itself: iOS only lets a page open another app from a tap.
- When the car corrected a place name it misheard, the card shows what it heard ("Heard "campy"" under Kamppi).
- The page also says when the cabin camera can't see. The destination is retained on `car/nav/destination`, so a page opened later still shows it; the button stays for 6 hours.

## Check-in menu (screen on ports 1-2, knob on ports 13-14)

- Turn the knob to move through the menu and press it to select.
- **I'm ok** opens: Send to mom, Send to dad, Send to both, Go back.
- Picking a recipient sends "I've arrived and everything is ok." plus the phone's location right away, using the last location from up to 30 minutes ago. If it's over 1 minute old, the hub also asks the phone for a new one and sends a second pin if the phone answers from more than 100 m away to that bot (`mom` and `dad` in `jetson-hub/secrets.json`). The screen then shows **Sent** (green), **Sent** without location (orange, no recent GPS fix) or **Failed** with the reason (red), and goes back to the menu. You can press the knob to close it sooner.
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
- On the Jetson, `vision.py` (part of `car-hub`) keeps the last 40 s of frames in RAM only. With an SOS, accident or threat alert, `hub.py` sends every linked chat a photo, an H.264 clip (960 px wide, about 15 KB per second of a still scene) and an AI incident report. If the camera is off, the alert and incident report go out without the photo and clip.
- The camera's auto exposure can get stuck and deliver black frames with the lens open. When frames stay near-black for 10 s, the streamer switches to manual exposure and back to auto, at most once a minute, and logs it (`journalctl -u safedrive-camera` on the Uno Q). `/health` shows `"dark": true` while frames are near-black: a covered lens, night, or a stuck camera.
- Keep the Uno Q off the Jetson's USB: there it shows up as a second serial port next to the Atech board.

### Incident report

- About 10–15 s after the alert (once the clip is ready, so it can come just before or after the video), `hub.py` sends a short plain-text summary from Gemini: what the clip shows inside the cabin, how many people are visible, the street address or nearest landmark, the nearest emergency department (from Google Maps), and one or two sentences to read to `112` (for a threat, it warns not to call the driver first).
- The message says it is AI-generated and can be wrong, and tells the family to check the clip. It describes only visible movement in the cabin and never diagnoses injuries; if the video is dark, blurry or missing, it says so instead of guessing.
- To keep it quick, three lookups run in parallel. As soon as the alert fires, OpenStreetMap (Nominatim) gives the exact street address at the phone's position (about 0.5 s) and Gemini with Google Maps finds the nearest emergency department. The third, a Gemini call with no tools that answers in JSON, describes the clip as soon as it's ready. Thinking is turned off for both Gemini calls. Gemini's own address is used only if OpenStreetMap has none: it names a nearby place, and often a different one each time.
- The hub log shows which Google Maps calls Gemini made and the places Maps returned (`Maps sources: ...`).
- Only when an SOS, accident or threat alert fires do the clip, the alert trigger, the cabin monitor's verdicts from that window and the car's location go to Gemini (and the coordinates to Google Maps and OpenStreetMap).
