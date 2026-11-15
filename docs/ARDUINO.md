# safeDrive on the Arduino UNO Q

**An in-car safety add-on using Arduino UNO Q, Modulino Thermo and Edge AI to detect occupants, watch cabin climate and send emergency alerts.**

**Team madpiece** · Junction 2026 · Best use of Arduino UNO Q · [▶ Demo video](https://gmli-eu.github.io/safeDrive/) · [Arduino Project Hub page](https://projecthub.arduino.cc/projects/f64f85a3-6f5f-4910-8ac1-9c4b1d8b2739) · Code: https://github.com/gmli-eu/safeDrive · [Presentation (PDF)](safeDrive_presentation.pdf)

This page is the full write-up for the Arduino team and judges. It covers the Modulino Thermo and the on-device person detection, which we added after the Project Hub page went into review. The [README](../README.md) gives the overview of the whole project.

## The problem

A crash, a sudden medical emergency, or a threatening passenger can leave a driver unable to reach their phone. When nobody in the car can call for help, the people who care about them find out too late, and emergency callers often lack an exact address or a clear picture of what happened inside the vehicle. Extreme cabin heat or cold also wears down driver attention and puts children, older passengers, and pets at risk.

Inside a moving car, looking at a phone screen to send a message, check a route, or talk across a language barrier takes the driver's eyes off the road. At the same time, drivers and passengers should not have to stream every word and every empty-car video frame to the cloud just to get smart assistance.

safeDrive is an add-on for any car that raises the alarm by itself when the driver cannot, monitors cabin climate and occupancy on the edge, and gives both the driver and back-seat passengers a hands-free, privacy-first voice and gesture interface.

## What safeDrive does

- **Discreet SOS button**: holding the button on port 6 for 2 s silently sends every linked Telegram contact a live location (or last known position up to 24 h old), a photo from the cabin camera, a 15 s video clip (10 s before to 5 s after), and an AI incident report.
- **Automatic accident detection**: a crash (> 2.5 g jolt) or rollover (tilted > 75° for 3 s) triggers a 10 s "Are you OK?" countdown on the driver's screen; if the driver does not press the knob to cancel, the full alert, clip, and report go out automatically.
- **Cabin threat watch and passenger hand signs**: NVIDIA Nemotron 3.5 Super VL watches for weapons, someone hitting, choking or restraining the driver, or someone grabbing the steering wheel, gear lever, or handbrake, triggering a silent alert; warns the driver out loud when their eyes close or get covered (and alerts their contacts after 10 s) or when they use their phone; it also reads seven passenger hand signs (all good, need a break, volume down, volume up, slower, faster, thank you) and speaks them to the driver.
- **AI incident report for 112**: within 10–15 s of an alert, Gemini combines a plain-text description of the cabin video clip, the exact street address from OpenStreetMap, the nearest emergency department from Google Maps, the cabin temperature and humidity, and the exact sentences to read to `112`.
- **Ask the car**: for 15 minutes after an alert, a contact can type a question to their Telegram bot ("Is he moving?") and Gemini answers in their language from the newest UNO Q camera frames, Nemotron's verdicts, the UNO Q's people count, the Modulino Thermo reading and the location. Nothing shows or sounds in the car; outside that window the bots answer no questions.
- **On-device occupancy and cabin climate warnings**: the Arduino UNO Q reads cabin temperature and humidity from the Modulino Thermo and counts people in the cabin offline with YOLOX-nano. When the cabin stays at 35 °C or above (or 0 °C or below) for 30 s and the UNO Q has seen someone in the last minute (or cannot tell because the picture is dark or the detector is down), the car asks the driver to turn on the AC or heating, reminds them every 5 minutes while it lasts (waiting if the car is busy talking), and shows a calm red banner on the phone page.
- **Offline-first driver voice assistant**: push-to-talk runs Whisper, Gemma 4 E2B, and Piper TTS locally on the Jetson to send Telegram messages, switch the cabin light, set phone navigation buttons, report cabin temperature, or answer questions—calling the online Gemini agent only when the user says "Google".
- **Back-seat intercom and accent-aware translator**: passengers have their own button, mic, and speaker to ask questions in their own language or talk with the driver through a two-way translator that matches regional language varieties (such as European vs. Brazilian Portuguese).

## How the Arduino UNO Q is used

The Arduino UNO Q is safeDrive's dedicated vision, climate, and edge-AI node. It connects to the car's Wi-Fi hotspot on its own and uses both processors on the board—the real-time STM32U585 microcontroller and the Linux application processor—communicating over the internal Arduino router.

### STM32U585 microcontroller: Modulino Thermo over Qwiic and Router Bridge
A **Modulino Thermo** is plugged into the UNO Q's Qwiic connector (`Wire1`). The sketch `unoq-camera/thermo/thermo.ino` runs on the STM32U585 microcontroller and reads temperature and relative humidity every 2 s using the `Arduino_Modulino` library. If the sensor is unplugged or returns an out-of-range reading, the sketch automatically re-initializes it on the next 2 s tick.

The same sketch also drives the UNO Q's built-in **LED matrix** (8 × 13, `Arduino_LED_Matrix`): it offers `matrix_show(people, cloud)`, which Linux calls through the router. The matrix shows the on-device people count as a digit ("?" when it can't tell) and a cloud icon while camera frames are going to Nemotron in the cloud. So data flows both ways between the two processors: sensor readings up, display commands down.

The microcontroller exposes a `thermo_read` RPC method to the Linux side through `Arduino_RouterBridge` (`Bridge.provide("thermo_read", thermo_read)`), returning the temperature in °C, humidity in %, and the reading age in milliseconds as a comma-separated string. Whenever `thermo.ino` changes, `tools/deploy.py unoq-camera` compiles and flashes the sketch directly on the UNO Q using `arduino-cli`.

### Linux side: USB camera server, exposure recovery, and RPC client
On the Linux side, `unoq-camera/stream.py` runs as a systemd service (`unoq-camera/safedrive-camera.service`) on port 8090 using only the Python standard library:
- **Camera capture (`capture_loop`)**: finds the **Arduino USB Camera** via `v4l2-ctl` (skipping the `qcom-venus` codec device), sets 50 Hz power-line anti-flicker, and reads raw 800×600 MJPEG frames at 10 fps without re-encoding (1280×720 at 15 fps took so much of the 2.4 GHz hotspot that the car's speech stuttered). It serves `/stream.mjpg`, `/preview.mjpg` (capped at ~2.5 fps to spare 2.4 GHz Wi-Fi bandwidth for speaker audio), `/frame.jpg`, `/health`, and the live web view with the interactive hand-sign guide (`/icons/*.png`).
- **Auto-exposure recovery (`kick_exposure`)**: if the camera's auto-exposure gets stuck and delivers near-black frames for 10 s, `stream.py` switches it to manual exposure and back to auto (at most once a minute), and flags `"dark": true` on `/health`.
- **Router Bridge client (`climate_loop`)**: every 2 s, `stream.py` packs a MessagePack-RPC request and sends it over the Unix domain socket `/var/run/arduino-router.sock` to call `thermo_read` on the STM32, serving the result as JSON at `/climate`.

### On-device Edge AI: YOLOX-nano person detection and two-tier AI architecture
`tools/deploy.py unoq-camera` starts Arduino's Edge Impulse model runner container (`ghcr.io/arduino/app-bricks/ei-models-runner:0.5.0`, shipped with Arduino App Lab) as a Docker container named `safedrive-people`, serving the **YOLOX-nano** model (`/models/ootb/ei/yolo-x-nano.eim`) on `127.0.0.1:1340`.

In `stream.py`, `occupancy_loop` posts the newest camera frame to `http://127.0.0.1:1340/api/image` about once per second (~0.5 s per frame, ~180 MB of RAM, completely offline) while the camera keeps streaming. `stream.py` serves the detection results at `http://172.20.10.4:8090/occupancy` (`people`, `confidence`, `empty_for_s`, `boxes`, and `ms`), which the Jetson polls every 2 s and publishes to `car/cabin/occupancy` (also exposed to the Gemini agent's `cabin_status` tool). On the UNO Q's live camera page (`http://172.20.10.4:8090/`), each detected person gets a bounding box labeled with its score (e.g. `person 0.86`)—drawn in green when the model is sure (confidence 0.5 or more) and yellow when unsure—plus a corner badge such as `On-device AI (Arduino UNO Q): 2 people · frames going to NVIDIA Nemotron · detection 519 ms on-device` (or `nothing sent to the cloud` in Arduino blue).

This creates a **two-tier AI architecture** between edge AI on the Arduino UNO Q and cloud reasoning on Verda:
- **Tier 1 — Always-on offline Edge AI (Arduino UNO Q)**: YOLOX-nano watches cabin occupancy locally all the time with nothing leaving the board.
- **Tier 2 — Fail-open cloud cabin watch (NVIDIA Nemotron 3.5 Super VL on Verda)**: Nemotron runs at full rate (at most one request every 2 s) while anyone might be in the car. The cabin counts as empty only after 10 s with no person boxes at all (even a low-confidence box resets the timer). Once empty for 10 s, Nemotron slows to one check every 15 s (`cabin_empty`, `EMPTY_PAUSE_S`, `WATCH_EMPTY_INTERVAL_S` in `jetson-hub/voice.py`), and returns to full rate immediately as soon as someone is seen. A dark picture or a stopped detector sets `people` to `null` ("can't tell") and never counts as empty.
- **Fail-open climate warnings**: the too-hot / too-cold spoken warning (`occupied` in `jetson-hub/hub.py`) speaks when the UNO Q has seen someone in the last minute, or when it cannot tell (detector down or picture too dark), so it never stays silent just because detection is unavailable.

## Architecture

```text
Arduino UNO Q (172.20.10.4:8090)
 ├─ STM32U585 MCU: thermo.ino ──Qwiic──> Modulino Thermo (temp & humidity every 2 s)
 │                  └──> built-in LED matrix (people count + cloud icon, set by matrix_show)
 │        │
 │   /var/run/arduino-router.sock (MessagePack-RPC via Arduino_RouterBridge)
 │        │
 └─ Linux: stream.py <──USB-C hub── Arduino USB Camera (800x600 MJPEG @ 10 fps)
          ├─ safedrive-people (ei-models-runner container on 127.0.0.1:1340, YOLOX-nano)
          └─ HTTP: /stream.mjpg, /frame.jpg, /climate, /occupancy, /health, live view
                   │
                   ▼  (Wi-Fi hotspot)
NVIDIA Jetson Orin Nano (172.20.10.2, WendyOS `car-hub`)
 ├─ vision.py: 40 s RAM frame ring buffer & H.264 clip recorder
 ├─ hub.py: Mosquitto MQTT, climate & occupancy watch, Telegram alerts, Gemini + Maps + OSM report, ask the car
 ├─ voice.py: Whisper STT, Gemma 4 E2B (Ollama), Piper TTS, Gemini agent, Nemotron watch
 └─ dash.py: safeDrive phone dashboard (:8092)
     ▲                                        │
     │ Wi-Fi (MQTT + UDP audio :1885)         ▼ HTTPS
Atech Driver Board & Back-Seat Board     Verda (Nemotron 3.5 Super VL), Gemini API, Telegram
```

## Demo: what the judges will see

1. **The UNO Q's LED matrix**: the board itself shows how many people its AI sees, and a cloud icon while frames go to Nemotron. Walk out of the camera's view: the digit drops to 0, and after 10 s the cloud only flashes every 15 s.
2. **Live person detection on the UNO Q**: open `http://172.20.10.4:8090/` to see the Arduino USB Camera's live stream with real-time bounding boxes around each person—green when confidence is 0.5 or more, yellow when lower—alongside the corner badges `On-device AI (Arduino UNO Q): 2 people · frames going to NVIDIA Nemotron · detection 519 ms on-device` (or `nothing sent to the cloud` in Arduino blue) and `Modulino Thermo: 26.4 °C · 32 % humidity`, the **What NVIDIA Nemotron sees** card (each cloud verdict, read from the Jetson) and the slide-out hand-sign guide.
3. **Cabin climate warning with Modulino Thermo**: we warm the Modulino Thermo with a fan (or lower `hot_car_c` in `jetson-hub/secrets.json` for a quick demo). Because the UNO Q has seen someone in the cabin within the last minute, the car speaks: *"It's getting hot in here: 36 degrees. Please turn on the air conditioning, especially with children, older passengers or pets on board."* At the same time, the safeDrive phone page (`http://172.20.10.2:8092/`) shows a calm red banner: *"Too hot inside, 36 °C. Turn on the AC."* and the driver's Atech screen shows the temperature in red.
4. **Hand signs and threat watch**: hold a thumbs-up or a T-shape ("need a break") toward the UNO Q camera and the car reads the message out to the driver. If someone grabs the wheel or shows a weapon, Nemotron triggers a silent threat alert. Close or cover your eyes and the car tells you to open them; pick up a phone and it asks you to put it down.
5. **SOS, crash countdown, and AI incident report**: hold the SOS button for 2 s or jolt the driver's board (> 2.5 g) to see the 10 s countdown on the screen. Within seconds, linked Telegram chats receive the live location, a cabin photo from the UNO Q, a 15 s H.264 video clip, and the Gemini incident report with the street address, nearest emergency department, cabin temperature, and `112` script.

## Results and what we measured

All numbers below were measured on the hardware during Junction 2026 (14 November 2026):

- **On-device person detection (YOLOX-nano on Arduino UNO Q)**: ~0.5 s per frame (~1 Hz detection rate), ~180 MB RAM usage, completely offline while the camera keeps streaming; slows the cloud watch to 1 check every 15 s after 10 s of an empty cabin.
- **Camera and climate node (Arduino UNO Q)**: streams 800×600 MJPEG at 10 fps while also running the person detector and reading the Modulino Thermo; a hot-cabin warning is spoken 30 s after the Thermo crosses 35 °C. `deploy.py unoq-camera` updates the board and checks frames, the Thermo and the people count in ~5 s (~1 min more when `thermo.ino` is rebuilt and flashed on the board).
- **Recovery after a power cut**: unplugged and plugged back in, the UNO Q brought everything back by itself, with no one touching it: camera stream, Modulino Thermo, sketch and LED matrix, then the Edge Impulse detector about a minute later. The Jetson and both Atech boards kept running and picked it up again. Ready about 2-3 minutes after power-on.
- **Voice command understanding (Gemma 4 E2B on Jetson, offline)**: 1.7–2.0 s.
- **Two-way regional translation (Gemini)**: 2.7–3.5 s per turn.
- **"Ask Google" agent with web search (Gemini)**: ~10 s.
- **AI incident report after an alert (Gemini + Google Maps + OpenStreetMap in parallel)**: ~10–15 s (OpenStreetMap street address lookup takes ~0.5 s; RAM video buffer holds the last 40 s of frames; H.264 clips are 960 px wide at ~15 KB/s for a still scene).
- **Answering a contact's question after an alert (Gemini)**: 3.4–6.8 s in our tests (15 November 2026).
- **Cabin watch (Nemotron 3.5 Super VL on Verda)**: about 1 s per 3-frame burst when the shared endpoint is not busy (longer when other teams load it), paced to at most one request every 2 s (~28 requests/min) when occupied, one every 15 s when empty for 10 s, and paused while the car speaks or when the camera is dark.

## Next steps

- **Protect children and pets in a parked car**: combine the UNO Q's on-device person detector and the Modulino Thermo to send an immediate Telegram alert if a child or pet is left inside a parked car that is getting dangerously hot or cold.
- **Watch the driver on the edge**: detect driver drowsiness, distraction, or a driver who slumps over, and warn them or alert their contacts.
- **Custom enclosures with Arduino and Atech.dev**: hide the UNO Q and Jetson in the glove compartment and mount custom-cased driver and back-seat consoles with a discreet SOS button.
- **Connect to the car over CAN bus / OBD-II**: read airbag deployment, speed, seatbelts, and doors for crash detection, and mute the car's radio automatically when safeDrive speaks or when a passenger signs "volume down".
- **eCall integration**: forward the structured incident report and GPS coordinates to Europe's eCall system or local emergency centres after a human confirmation step.

## Components and supplies

- **Arduino UNO Q** (2 GB RAM, 16 GB eMMC, dual-processor STM32U585 + Linux, built-in 8 × 13 LED matrix)
- **Arduino USB Camera**
- **Arduino Modulino Thermo** (connected over Qwiic)
- **USB-C hub (8-in-1)** (connected to the UNO Q's USB-C port)
- **45 W USB-C power supply** and **USB-C cable**
- **NVIDIA Jetson Orin Nano** (from Wendy, running WendyOS)
- **Atech 14-port board (ESP32-S3) — driver console**: `st7735_tft` screen (ports 1–2), `microphone` (ports 3–4), voice `button` (port 5), SOS `button` (port 6), `neopixel` light grid (port 7), `icm40608` IMU orientation sensor (port 9), `speaker` (ports 10–11), `rotary_encoder` knob (ports 13–14)
- **Atech 14-port board (ESP32-S3) — back-seat intercom**: `speaker` (ports 3–4), `button` (port 9), `microphone` (ports 13–14)
- **iPhone** (2.4 GHz Wi-Fi hotspot, GPS location, and the safeDrive dashboard page in the phone holder)

## Apps and platforms

- **Arduino App Lab** (`ei-models-runner` container) and **arduino-cli** (with the `Arduino_Modulino`, `Arduino_RouterBridge` and `Arduino_LED_Matrix` libraries)
- **Edge Impulse** (YOLOX-nano object detection model running on the Arduino UNO Q)
- **NVIDIA Nemotron 3.5 Super VL** (`nemotron-3.5-super-vl` on the Verda Inference API)
- **Google Gemma 4 E2B** (`gemma4:e2b` via Ollama, running offline on the Jetson)
- **Google Gemini API** (`gemini-flash-latest`, `gemini-3.8-flash`, `gemini-3.8-flash-tts`) with **Google Search** and **Google Maps** grounding
- **OpenStreetMap** (Nominatim reverse geocoding)
- **Whisper** (`faster-whisper` `small.en` and `small`) and **Piper TTS** (local voice synthesis)
- **WendyOS** and **Wendy CLI** (container orchestration on the Jetson)
- **Atech platform** (`atech==1.0.0a12` and PlatformIO)
- **Eclipse Mosquitto** (MQTT broker), **Telegram Bot API**, and **OwnTracks**
