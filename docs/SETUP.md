# Setup and deploy

How safeDrive is built, and how to set it up and deploy it to the hardware. What each feature does is in [FEATURES.md](FEATURES.md); the overview is in the [README](../README.md).

## Architecture

```
Atech driver board ─────┐  Wi-Fi: MQTT + UDP audio                     ┌─> Telegram (alerts, messages, check-ins)
Atech back-seat board ──┼──────────────────────> Jetson Orin Nano ─HTTPS─┼─> Gemini API (agent, translator, incident report)
UNO Q + camera, Thermo ─┤  MJPEG, JSON / HTTP    hub.py, voice.py,       ├─> Nemotron 3.5 Super VL on Verda (cabin watch)
iPhone (location) ──────┘  MQTT / Telegram       vision.py, dash.py,     └─> Google Maps, OpenStreetMap
                                                 Ollama: Gemma 4 E2B (offline)
```

| Folder           | Runs on                            | What it does |
| ---------------- | ---------------------------------- | ------------ |
| `jetson-hub/`    | Jetson (Wendy app `car-hub`)       | Mosquitto broker; `hub.py`: Telegram alerts, location, incident report; `voice.py`: voice assistant, translator, cabin watch; `vision.py`: camera buffer and clips; `dash.py`: the safeDrive page for the driver's phone, and Nemotron's recent verdicts for the UNO Q's live view (`/nemotron`) |
| `atech-flasher/` | Jetson (Wendy app `atech-flasher`) | Builds the Atech firmware for both boards (`project.yaml`: driver, `passenger.yaml`: back seat), flashes each over USB, then streams their output |
| `unoq-camera/`   | Arduino UNO Q (systemd service + sketch) | `stream.py`: serves the USB camera and its live view (with the What NVIDIA Nemotron sees card), the cabin climate (`/climate`) and the on-device people count (`/occupancy`) over HTTP on port 8090; `thermo/thermo.ino`: on the microcontroller, reads the Modulino Thermo and drives the LED matrix |
| `tools/`         | dev machine                        | `deploy.py` checks an app's web page, deploys the app and waits until it's up; `mqtt_peek.py` watches the broker; `eval_watch.py` records labelled scenes and scores Nemotron's cabin watch on them |

## Setup

1. **Secrets** (all ignored by git):
    - `jetson-hub/secrets.json`: copy `secrets.example.json` and add the bot tokens from BotFather. For "ask Google" and the AI incident report, add `gemini_api_key` (from aistudio.google.com), or put `GEMINI_API_KEY=...` in the repo's `.env`; without it everything else still works.
    - `atech-flasher/secrets.json`: copy `secrets.example.json` and add the Wi-Fi name and password.
    - `unoq-camera/secrets.json`: copy `secrets.example.json` and add the Uno Q's Linux password and its address on the hotspot.
2. **Wi-Fi**: the board only sees 2.4 GHz. On an iPhone hotspot, turn on _Maximize Compatibility_. The firmware expects the Jetson at `172.20.10.2` (`MQTT_HOST` in `atech-flasher/project.yaml`).
3. **Phone location**: install OwnTracks and set Mode MQTT, Host `172.20.10.2`, Port `1883`, TLS off, Authentication off, location access _Always_, monitoring _Move_. Turn on remote commands (`cmd`) too: when the hub's last location is over 1 minute old, it asks the phone for a new one.
4. **Telegram**: each contact opens their bot and taps **Start**. The bot replies "✅ SafeDrive connected".
5. **Live location through Telegram** (works better than OwnTracks on an iPhone): on the driver's phone, open one of the safeDrive bots, tap **Start**, then 📎 → Location → **Share My Live Location** → **Until I turn it off**. The hub takes locations only from chats linked to a bot in `jetson-hub/secrets.json`. Telegram only sends a new position when the phone moves, so while sharing is on, the last position counts for up to 6 hours (`LIVE_SHARE_MAX_AGE`). If sharing stops, it counts for 30 minutes like an OwnTracks fix.
6. **safeDrive page**: on the driver's phone, open `http://172.20.10.2:8092/` in Safari and add it to the home screen. Set **Settings → Display & Brightness → Auto-Lock → Never**: the page can't keep the screen on by itself, because the Wake Lock API only works on HTTPS pages, and a silent-video workaround doesn't work on current iOS.

## Deploy

```sh
python3 tools/deploy.py jetson-hub      # ~2 s once cached
python3 tools/deploy.py atech-flasher   # ~1-2 min: build, flash both boards, wait for each board's MQTT connection
python3 tools/deploy.py unoq-camera     # ~5 s: copy stream.py to the Uno Q over SSH, restart, wait for frames, a Thermo reading and a people count
                                        # (~1 min more when thermo.ino changed: built and flashed on the Uno Q)
```

Before deploying `unoq-camera` or `jetson-hub`, `deploy.py` checks their web pages (the `PAGE` in `stream.py` and `dash.py`): the page must encode as UTF-8, and its JavaScript must pass `node --check`. Otherwise it stops without deploying and shows the error. Without `node`, only the encoding is checked.

`deploy.py jetson-hub` first checks the hub log and refuses to deploy while an SOS, accident, threat or eyes-closed alert from the last 2 minutes hasn't finished sending its clip and incident report, because a restart would drop them. `--check` runs only that check; `--force` deploys anyway.

`deploy.py` runs the app's `make_compose.py`, which generates `compose.yml` with the code and secrets embedded. Then it runs `wendy run --detach` and follows the logs until the app is up. The device defaults to `wendyos-hackathon-device-4.local`; set `WENDY_DEVICE` to override it. Each run's `compose.yml` is generated and holds secrets, so it isn't committed.

The first start of `jetson-hub` downloads about 8 GB (Ollama image, Gemma, Whisper and Python packages) into the app's volumes. `voice.py` downloads the LLM itself, into Ollama's store, over IPv4: Ollama's own pull uses IPv6, which the phone hotspot keeps resetting. Later starts don't download anything.

## Evaluating the cabin watch

`tools/eval_watch.py` measures how well Nemotron's cabin watch does what the car needs, on scenes acted out in front of the UNO Q camera. It runs on the dev machine and needs ImageMagick's `convert` (or Pillow) and the Verda key (`VERDA_INFERENCE_KEY` in `.env`, or `verda_inference_key` in `jetson-hub/secrets.json`).

```sh
python3 tools/eval_watch.py cases                      # the scenes, and what the car should do with each
python3 tools/eval_watch.py record                     # act out every scene, 5 bursts each (about 10 min)
python3 tools/eval_watch.py record eyes_hands phone_call --reps 3   # only some scenes
python3 tools/eval_watch.py run --name neck-eyes-phone # score the prompt in the working tree
python3 tools/eval_watch.py run --rev cbc586e --name before        # score the prompt from an older commit
python3 tools/eval_watch.py compare eval/results/*.json            # runs side by side
python3 tools/eval_watch.py compare A.json B.json --exclude knife    # leave a scene out (also works with run)
```

- **Scenes** (`CASES` in `eval_watch.py`): normal driving and an empty seat; things that must not raise anything (talking with the hands, chin on a fist, scratching the neck, looking down, blinking, sunglasses, holding a phone without looking at it); the choking sign, hands reaching around the neck from behind it (done by the driver: from the front they look like an attacker's), a scarf or belt across the throat, a knife and a pen held the same way; closed eyes, a slumped head, and eyes covered by hands, an object or a hood; using a phone and a phone call; the seven hand signs. Two scenes need a second person behind the driver (`choke_behind`, `arm_neck`): `record` only takes them when they're named.
- **Recording**: each burst is 3 frames 0.75 s apart, shrunk to 640 px, exactly what the cabin watch sends. They're saved in `eval/data/<scene>/`, which git ignores because they show people's faces. Delete a burst's folder if the scene went wrong. While it records, the tool pauses the car's own cabin watch (`car/vision/pause`, renewed every 30 s, at most 120 s per message, so the watch comes back by itself), and a staged choking or closed-eyes scene doesn't alert anyone.
- **Scoring**: every burst is checked on every task, as the car would act on its verdict (`car_actions` in the prompt block, the same function the car runs; older revisions without it are scored by the rules the car had then, in `predicted` in `eval_watch.py`): threat alert (`level` is danger, or both hands, an arm or a cord on the throat), eyes warning (`eyes` closed or covered), phone reminder (`phone` using) and the hand sign. A thumbs up that raised a threat alert counts as a false alarm. The tool prints recall and false alarms per task, the scenes it got wrong with what Nemotron said, and the median time per verdict, and saves everything in `eval/results/` (no images).
- **Prompt versions**: `run` reads the prompt, schema and request from the block between the `watch prompt` markers in `jetson-hub/voice.py`, so it tests exactly what the car runs. `--rev` takes it from any commit, to measure a prompt change.

## Two boards on the Jetson's USB

- **Board 1, the driver's** (`atech-flasher/project.yaml`): everything below except the intercom.
- **Board 2, the back-seat intercom** (`atech-flasher/passenger.yaml`): button on port 9, speaker on ports 3-4, microphone on ports 13-14.
- The flasher tells them apart by listening to each USB port for a few seconds: the driver board streams its orientation sensor, the passenger board its mic level (`rearmic_level`). It also remembers each board's USB serial number in its cache. A blank board is taken to be whichever one is missing. If it can't tell them apart, unplug the passenger board, deploy, plug it back in and deploy again.
- Port 12 is the Reset button: Atech has no pins for it, so a double-width module can't use ports 11-12 (`atech validate` rejects it). The right column's pairs are 9-10, 10-11 and 13-14.
- Keep the Uno Q off the Jetson's USB: there it shows up as a second serial port next to the Atech board.

## Camera (Arduino UNO Q)

- The camera goes into a powered USB-C hub on the Uno Q's only USB-C port. The Uno Q joins the hotspot by itself and serves the camera at `http://172.20.10.4:8090/`.
- Resolution and frame rate: `WIDTH`, `HEIGHT` and `FPS` in `unoq-camera/safedrive-camera.service` (set to 800×600 at 10 fps: 1280×720 at 15 fps took so much of the 2.4 GHz hotspot that the speaker's audio stuttered).
- Logs: `journalctl -u safedrive-camera` on the Uno Q.
- **After power-on**, everything starts by itself (tested by unplugging the board): the camera is back on the Jetson about 1 minute after the Uno Q rejoins the hotspot, the Thermo a few seconds later, and the person detector about a minute after that. Meanwhile the LED matrix shows "?" with the cloud icon (it can't tell yet, so the cloud watch keeps its full rate). Give it 2-3 minutes before a demo. The camera may come back under another `/dev/video` number; the streamer finds it by name.

## Cabin climate (Modulino Thermo)

- Plug the Modulino Thermo into the Uno Q's Qwiic connector. The sketch `unoq-camera/thermo/thermo.ino` runs on the microcontroller and answers `thermo_read` through the Arduino router (it also offers `matrix_show` for the LED matrix, see below); `stream.py` asks it every 2 s over `/var/run/arduino-router.sock` and serves `http://172.20.10.4:8090/climate`.
- `deploy.py unoq-camera` installs the `Arduino_Modulino` and `Arduino_RouterBridge` libraries on the Uno Q if they're missing, then builds and flashes the sketch with `arduino-cli`, only when it changed (`~/safedrive-camera/thermo/.flashed` holds the hash of the last flash).
- If `/climate` reports `"Modulino Thermo not found"`, check the cable; the sketch looks for the sensor again every 2 s. If it reports a router error, the sketch isn't running: delete `.flashed` on the Uno Q and deploy again.
- The hub reads `/climate` next to `camera_url` (override with `climate_url`) and publishes `car/cabin/climate` (retained).

## On-device person detection (Edge Impulse on the Uno Q)

- `deploy.py unoq-camera` starts Arduino's Edge Impulse model runner as a Docker container named `safedrive-people` (image `ghcr.io/arduino/app-bricks/ei-models-runner:0.5.0`, already on the Uno Q with Arduino App Lab), serving YOLOX-nano (`/models/ootb/ei/yolo-x-nano.eim`) on `127.0.0.1:1340`. Docker restarts it with the board.
- `stream.py` posts the newest frame to it about once a second and serves `http://172.20.10.4:8090/occupancy`: `people` (boxes at 0.5 or more), `confidence`, `empty_for_s`, `boxes` (0-1, relative to the frame) and `ms`. `people` is `null` while the picture is too dark or the detector isn't answering.
- The hub polls it every 2 s and publishes `car/cabin/occupancy` (override the address with `occupancy_url`). `voice.py` slows Nemotron to one check every 15 s after 10 s of an empty cabin (`EMPTY_PAUSE_S`, `WATCH_EMPTY_INTERVAL_S`).
- Logs: `docker logs safedrive-people` on the Uno Q.
- **LED matrix**: `matrix_loop` in `stream.py` calls the sketch's `matrix_show(people, cloud)` through the router when the count or the cloud state changes (and every 10 s). The cloud state comes from the hub's poll (`/occupancy?cloud=1`): `voice.py` publishes `car/vision/sending` for each Nemotron request, and the hub counts frames as "going to the cloud" for 5 s after each, or while a request waits for its answer. Matrix changes are logged as `matrix: 2 + cloud`.

## Configuration

Optional keys in `jetson-hub/secrets.json`:

| Key | What it changes | Default |
|---|---|---|
| `gemini_api_key` | Gemini, for "ask Google", the translator and the incident report (or `GEMINI_API_KEY` in `.env`) | none: those features are off |
| `verda_inference_key` | Nemotron cabin watch on Verda (or `VERDA_INFERENCE_KEY` in `.env`) | none: cabin watch is off |
| `llm_model` | Local LLM on Ollama | `gemma4:e2b` |
| `whisper_model` | Driver's speech-to-text | `small.en` |
| `whisper_multi_model` | Back seat's speech-to-text (any language) | `small` |
| `piper_voice` | Driver's voice | `en_US-lessac-medium` |
| `piper_voices` | Back-seat voice per language, e.g. `{"pt": "pt_BR-faber-medium"}` | `PIPER_VOICES` in `voice.py` |
| `prefetch_langs` | Back-seat voices downloaded at start (list) | Portuguese |
| `gemini_model` | Gemini agent model | `gemini-flash-latest` |
| `translate_model` | Translator model | `gemini-3.8-flash` |
| `tts_model`, `tts_voice` | Gemini TTS for varieties with no local voice | `gemini-3.8-flash-tts`, Kore |
| `gesture_from` | Who can make hand signs: `"any"` (demos at a table) or `"back"` (real car: back seat only, spoken as "From the back seat: ...") | `"any"` |
| `phone_warn` | Ask the driver to put their phone down when Nemotron sees them using it. Set `false` for a demo where the "driver" holds a phone to show the live view | `true` |
| `voice_peak` | Peak level of spoken answers; lower it if the sound breaks up | 0.75 |
| `voice_drive` | Compression of spoken answers (0 = off, 3 = louder but harsher) | 2.0 |
| `hot_car_c`, `cold_car_c` | Cabin temperature (°C) at or beyond which the car asks for the AC or the heating | 35, 0 |
| `cabin_hold_s` | How long the cabin must stay that hot or cold first | 30 |

Spoken answers are made louder in software (soft compression, peaks at 75 % of full scale) because the board's speaker volume can't go above 0.6 without clipping, and full-scale speech broke up.

Accident thresholds are in `atech-flasher/project.yaml`: `CRASH_G`, `FLIP_DEG`, `FLIP_VOICE_GRACE_MS`. In `jetson-hub/hub.py`: `EMERGENCY_NUMBER` (default `112`), `LIVE_SHARE_MAX_AGE`, `LAST_KNOWN_MAX_AGE` and `CABIN_REMIND_S` (how often the too hot / too cold warning repeats, default 300 s). In `jetson-hub/voice.py`: `WATCH_MIN_INTERVAL_S`, the shortest time between two Nemotron requests (default 2 s; lower it only with a fast network, or the speech breaks up).
