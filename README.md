# safeDrive

**When you can't call for help, your car does.**

**Team madpiece** · Junction 2026 · [Arduino Project Hub page](https://projecthub.arduino.cc/projects/f64f85a3-6f5f-4910-8ac1-9c4b1d8b2739/) · [Arduino UNO Q write-up](docs/ARDUINO.md) · [Presentation (PDF)](docs/safeDrive_presentation.pdf)

A crash, a medical emergency or a threatening passenger can leave a driver unable to reach their phone, and the people who care about them find out too late. safeDrive is an add-on that makes the car raise the alarm by itself. A hidden SOS button, a crash or rollover, or a threat spotted by the cabin camera sends the driver's chosen contacts, within seconds, a live location, a photo, a video clip of the moments before, and an AI incident report with the street address, the nearest emergency department and exactly what to tell `112`. For the next 15 minutes, the contacts can ask the car what's happening now ("Is he moving?") and get an answer from the camera and the car's sensors.

Inside the car, safeDrive keeps the driver's eyes on the road. A voice assistant that runs offline sends messages, finds directions and answers questions. A back-seat intercom translates between passengers and the driver, down to their regional accent. If the driver's eyes close or get covered, or they pick up their phone, the car tells them out loud. Passengers can ask for a break or a slower ride with a simple hand sign. An AI model on the Arduino UNO Q itself knows who's in the car, offline, and the car asks for the AC or the heating when the cabin gets too hot or cold.

<p align="center">
  <img src="docs/images/car-setup.jpg" alt="Illustration of safeDrive in a car: the Jetson, the Arduino UNO Q and its camera on the dashboard, the driver's Atech board on the centre console, and the back-seat Atech board on the back of the passenger seat" width="320">
  <br><em>How the safeDrive hardware could fit in a car (AI-generated illustration). In a real car, the Jetson and the UNO Q would be hidden in the glove compartment, and the driver's and back-seat boards would sit in custom-made cases. Here everything is shown in the open so you can see every device we use.</em>
</p>

**More detail:** [docs/FEATURES.md](docs/FEATURES.md) explains how each feature behaves. [docs/SETUP.md](docs/SETUP.md) covers the architecture, setup, deploy and configuration.

## What it can do

**Safety alerts.** Each one goes to every linked Telegram chat, with live location, a camera photo, a video clip and an AI incident report.

- **SOS button**: hold it for 2 s. Discreet: no sound, only two brief amber flashes when the alert goes out. ([details](docs/FEATURES.md#sos-button-port-6))
- **Accident detection**: a crash (jolt over 2.5 g) or a rollover (tilted over 75° for 3 s) starts a 10 s "Are you OK?" countdown on the screen. If nobody presses the knob to cancel, the alert goes out. ([details](docs/FEATURES.md#accident-detection-orientation-sensor-on-port-9))
- **Threat in the cabin**: Nemotron watches the camera and silently alerts the driver's contacts to a weapon, someone hitting, choking or restraining the driver, or someone grabbing the wheel, gear lever or handbrake. ([details](docs/FEATURES.md#cabin-watch-nvidia-nemotron-35-super-vl))
- **Eyes closed or covered at the wheel**: when Nemotron sees the driver's eyes shut, or covered by hands, an object or a hoodie, for more than a blink, the car tells them out loud, and after 10 s it alerts their contacts: they may have fallen asleep, passed out, or can't see the road. ([details](docs/FEATURES.md#cabin-watch-nvidia-nemotron-35-super-vl))
- **Phone down**: when Nemotron sees the driver using a phone, the car asks them to put it down and offers to send the message by voice instead. ([details](docs/FEATURES.md#cabin-watch-nvidia-nemotron-35-super-vl))
- **Who's in the car, worked out on the Arduino**: an Edge Impulse model on the UNO Q counts the people in the cabin about once a second, offline, and shows the count on the board's own LED matrix and the camera's live view, with a cloud icon (and NVIDIA green instead of Arduino blue) whenever frames are going to Nemotron. In an empty car, the cloud cabin watch slows to one check every 15 s; it never slows down while someone might be inside. ([details](docs/FEATURES.md#camera-arduino-uno-q--arduino-usb-camera))
- **AI incident report**: Gemini describes the clip, gives the street address, finds the nearest emergency department with Google Maps and writes what to say to `112`. ([details](docs/FEATURES.md#incident-report))
- **Ask the car**: for 15 minutes after an alert, a contact can type a question to their Telegram bot ("Is he moving?", "Is anyone else in the car?", "Is it hot in there?") and Gemini answers in their language, from the newest camera frames, Nemotron's cabin verdicts, the people count, the cabin temperature and the location. Nothing shows or sounds in the car, so even after a silent threat alert, nobody has to call the driver to find out. At any other time the bots answer no questions. ([details](docs/FEATURES.md#ask-the-car-after-an-alert))
- **Last known position**: if the phone has gone quiet, alerts still send its last position (up to 24 h old), clearly marked as old.

**Voice assistant for the driver.** Press the voice button and speak; no wake word. ([details](docs/FEATURES.md#voice-assistant-button-on-port-5-mic-on-ports-3-4-speaker-on-ports-10-11))

- Send a Telegram message to a contact ("send a message to mum, I'm running late"); if it's unclear who, the car asks.
- Turn the cabin light on or off.
- Directions: puts a **Go to ...** button on the driver's phone that opens Google Maps or Waze.
- Quick facts, jokes and small talk from Gemma 4, running offline in the car.
- **Too hot or too cold inside**: a Modulino Thermo on the UNO Q measures the cabin. When it stays at 35 °C or more (or 0 °C or less) for 30 s and someone is in the car, the car asks the driver to turn on the AC (or the heating), especially with children, older passengers or pets on board, with a reminder every 5 minutes while it lasts. It never talks over the driver: if the car is busy, the warning waits. The safeDrive page shows a calm red banner. ([details](docs/FEATURES.md#too-hot-or-too-cold-inside-modulino-thermo-on-the-uno-q))
- "Ask Google ...": a Gemini agent with Google Search, Google Maps and the car's own functions, chained in one request ("find the nearest open pharmacy and text mum I'm stopping there").

**Back seat**

- **Intercom**: the passenger's own button and speaker. They ask questions in any language and hear the answer in that language. Questions only: no messages or car controls. ([details](docs/FEATURES.md#back-seat-intercom-board-2-button-on-port-9-speaker-on-ports-3-4-mic-on-ports-13-14))
- **Translator**: the driver turns it on from the screen menu. The passenger speaks their language and the driver hears English; the driver's reply is spoken back in the passenger's exact regional variety (Portugal vs Brazil, Spain vs Mexico). ([details](docs/FEATURES.md#translator-drivers-screen-menu--translator))
- **Hand signs**: seven signs (all good, need a break, volume up, volume down, slower, faster, thank you) that the car reads out to the driver. ([how to make them](docs/FEATURES.md#hand-signs-to-try))

**Check-in and phone**

- **I'm ok**: pick it on the screen with the knob to send "I've arrived and everything is ok." with the location to one or all contacts. ([details](docs/FEATURES.md#check-in-menu-screen-on-ports-1-2-knob-on-ports-13-14))
- **safeDrive page** on the phone in the holder: shows the destination button, the cabin temperature (with a calm red banner when it's too hot or too cold) and whether the cabin camera can see. ([details](docs/FEATURES.md#safedrive-page-phone-in-the-holder))
- **Location** from Telegram live location or OwnTracks; the hub asks the phone for a fresh fix when the last one is over 1 minute old.

## Feedback so far

We demoed the SOS button, accident detection and the AI incident reports to several of the hackathon's tech partners and to other participants. What impressed people most was how fast the photo, the video clip and the AI report arrived on Telegram after the trigger. Everyone found it useful, and one suggested pitching it to a Chinese carmaker.

## Hardware

| Device | Role |
|---|---|
| NVIDIA Jetson Orin Nano (from Wendy, running WendyOS) | The car's brain: MQTT broker, alerts, Gemma 4, Whisper and Piper, camera buffer |
| Atech 14-port board (ESP32-S3), driver | Screen, knob, SOS and voice buttons, mic, speaker, light, orientation sensor |
| Atech 14-port board (ESP32-S3), back seat | Button, mic and speaker for the intercom and translator |
| Arduino UNO Q + Arduino USB Camera + Modulino Thermo | Cabin camera, streamed over Wi-Fi; people in the cabin, counted on the device (Edge Impulse) and shown on its LED matrix; cabin temperature and humidity |
| iPhone | Wi-Fi hotspot, location, and the safeDrive page in the phone holder |

## Privacy

- **Your voice never leaves the car by default.** Speech-to-text (Whisper), the assistant's understanding and answers (Gemma 4) and its voice (Piper) all run locally on the Jetson, with no cloud service involved: they keep working with no internet connection at all.
- What someone says goes to Gemini only when they say "Google" (or "Gemini"), or while the driver has the translator on.
- Camera frames go to Nemotron on Verda for the cabin watch. In an empty parked car, far fewer do: the UNO Q's on-device person detection, which never sends anything out, slows the cloud watch to one check every 15 s. You can see when frames leave the car: a cloud icon lights up on the UNO Q's LED matrix, and the people count turns from Arduino blue to NVIDIA green on the live view and the safeDrive page. A clip and the location go to Gemini only when an SOS, accident, threat or eyes-closed alert fires. For 15 minutes after that alert, the newest camera frames also go to Gemini each time a contact asks the car a question ("ask the car"). At any other time the bots answer no questions, so nobody can use them to watch the driver. Nemotron's verdicts are shown on the UNO Q's live view, never on the driver's phone page, so a silent threat alert stays silent.
- Camera frames are kept in RAM only, for the last 40 s.
- The cabin temperature stays in the car. It only goes out inside an incident report, or an answer to a contact's question, after an SOS, accident, threat or eyes-closed alert.

## What's used where

Challenge requirements are in [HACKATHON.md](HACKATHON.md). Each section lists the technology, what it does in safeDrive, and the files and functions to read.

### Best Use of Nemotron

- **Model**: NVIDIA Nemotron 3.5 Super VL (`nemotron-3.5-super-vl`), on the Verda-hosted endpoint (`NEMOTRON_URL` in `jetson-hub/voice.py`).
- **What it does**: the cabin watch. `watch_loop` in `jetson-hub/voice.py` keeps sending a burst of 3 frames from the last 1.5 s, at most one request every 2 s, and none while the car is speaking. Nemotron returns a JSON verdict (`nemotron`, structured output with a JSON schema): danger level, what it sees, how many people, its hands, and any hand sign.
- **Where you can see it**: the UNO Q's live view shows **What NVIDIA Nemotron sees** next to the camera picture: each verdict as Nemotron wrote it, when it made the car speak, and how fast it came back (`/nemotron` in `jetson-hub/dash.py`, the `nemo` panel in `unoq-camera/stream.py`).
- **Why it matters**: a threat (a danger verdict, or hands, an arm or a cord on the driver's throat) triggers a silent alert to the driver's contacts with photo, clip and incident report (`car/vision/threat` → `send_threat` in `jetson-hub/hub.py`). A phone in the driver's hand gets a spoken reminder to put it down (`watch_phone`). Closed or covered eyes make the car warn the driver out loud, then alert the contacts after 10 s (`watch_eyes` in `voice.py`, `send_unresponsive` in `hub.py`). Hand signs are spoken to the driver. The Gemini agent can call `cabin_status` to read Nemotron's latest verdict.
- **Improving its output**:
    - A narrow definition of danger (weapon, hitting, restraining, grabbing the controls), so blocked views and normal movement don't trigger alerts.
    - **Describe first, then classify.** The schema's fields are generated in order, with thinking off, so every category comes after the description it depends on: `hands` first, then `neck`, `eyes`, `phone`, and the danger `level` last. With `level` first, Nemotron answered "none" to both hands on the driver's throat, then described them as "a thinking pose". With `neck` before `hands`, it described "both hands clasped around the neck" and still answered one hand.
    - **The car decides in code, not the model.** `car_actions` in `voice.py` turns each verdict into what the car does. Both hands, or an arm or cord, on the throat raise the alert even when Nemotron's own `level` says "none", which it did for 4 of 5 bursts of hands reaching around the neck from behind. An attacker behind the seat only shows their hands, which look like the driver's own, so they count whoever's they seem to be.
    - A sign is spoken once 2 of the last 3 verdicts agree. When the shared endpoint is slow (5 s or more per verdict), one verdict is enough, so nobody has to hold a sign for a minute.
    - Dark or missing frames are never sent (`covered`).
    - Paced to one request every 2 s and paused while the car speaks: when the endpoint answered in 1 s, back-to-back uploads over the phone hotspot broke up the speaker's audio.
    - Temperature 0 and a fixed schema.
    - `eyes` and `phone` fields with their own rules in code, rather than one danger level: closed or covered eyes need all 3 frames (longer than a blink), phone use 2 of the last 3 verdicts (longer than a glance), and "looking down" and "holding" don't count, so the car only speaks when it should.
    - **Measured, not guessed**: `tools/eval_watch.py` records labelled scenes in front of the camera (choking, closed and covered eyes, phone use, the hand signs, and look-alikes that must not trigger anything) and scores each prompt version on them, with recall and false alarms per task ([how](docs/SETUP.md#evaluating-the-cabin-watch)). Results below.

#### Evaluation results

27 scenes acted out in front of the UNO Q camera, 5 bursts each (135 bursts, 3 frames each, exactly what the car sends), scored on what the car would do with each verdict. Three prompt versions on the same bursts, 15 November 2026 (`eval/results/20261115-0029-before.json`, `-0030-neck-eyes-phone.json`, `-0027-hands-first-v2.json`):

| | Before (danger level first) | + neck, eyes, phone fields | + hands first, decision in code (running now) |
|---|---|---|---|
| Threat alert: caught | 4 of 15 | 0 of 15 | **13 of 15** |
| Threat alert: false alarms | 0 of 120 | 0 of 120 | **0 of 120** |
| Eyes closed or covered: caught | 0 of 25 | 25 of 25 | **25 of 25** |
| Eyes closed or covered: false alarms | 0 of 110 | 3 of 110 | 7 of 110 |
| Phone use: caught | 0 of 10 | 9 of 10 | **10 of 10** |
| Phone use: false alarms | 0 of 125 | 4 of 125 | 6 of 125 |
| Hand signs right | 21 of 35 | 23 of 35 | **25 of 35** |
| Signs where there was none | 3 of 100 | 1 of 100 | 1 of 100 |
| Median time per verdict | 1.0 s | 1.1 s | 1.0 s |

What the numbers don't show, from looking at the frames:

- **Threats**: the scenes are the choking sign, both hands reaching around the neck from behind (from the front they look like an attacker's), and a cord pulled across the throat. Adding the neck field alone made it worse (0 of 15): Nemotron picked the neck category before describing the hands. Describing the hands first and deciding in code fixed it. The two misses are one hand at the throat without visible distress, and fingers interlaced just under the chin. A weapon wasn't tested: we had no knife at the venue, and a pen held up is (correctly) called a pen.
- **Eyes**: 5 of the 7 false alarms are the "need a break" T sign, whose flat hand really is across the signer's eyes. In a real car the signs come from the back seat, not the driver.
- **Phone**: 5 of the 6 false alarms are a phone held up at face height while looking at the camera. Holding a phone while driving is illegal in Finland and most of the EU anyway, so a reminder there does no harm.

### Best Use of Gemini API

- **The problem, and why it matters**: when a driver is hurt, asleep at the wheel or threatened, the people alerted are far away, scared and blind. They need to know what happened, where, and what to do, fast. After a threat alert they can't even call the driver: a ringing phone could put them at risk. A pin on a map and a raw video aren't enough when someone has to decide within a minute whether to call `112`.
- **How we integrated it** (all with the Gemini REST API, thinking off so answers come fast):
    - **Ask the car** (`ask_car`, `answer_question`, `ask_context` in `jetson-hub/hub.py`): for 15 minutes after an alert (`ASK_WINDOW`, as long as the live location), any linked Telegram chat can type a question. Gemini gets 4 camera frames from the last 4 s (`/frames` in `jetson-hub/vision.py`), the last minute of Nemotron cabin verdicts, the on-device people count, the cabin temperature, the street address and a Google Maps link, the alert and the incident report already sent, and the chat's last 4 questions and answers, so follow-ups work. Telegram shows "typing..." while it works; answers took 3.4-6.8 s in our tests. One question at a time per chat, and nothing happens in the car: no sound, no screen, no message to the boards.
    - **Incident report** (`jetson-hub/hub.py`): `describe_clip` sends the alert's video clip to Gemini for a description of what happened in the cabin. `where_and_er` uses Gemini with Google Maps grounding to find the nearest emergency department. `send_report` combines both with the street address into one message, including the sentences to read to `112`. All three lookups run in parallel, so the report lands about 10-15 s after the alert.
    - **Agent** (`ask_google`, `gemini_call`, `run_function` in `jetson-hub/voice.py`): Gemini with Google Search, Google Maps grounding at the car's location, and five car functions (`send_message`, `set_light`, `show_directions`, `cabin_status`, `cabin_climate`).
    - **Translator** (`translate_turn`, `gemini_hear`, `gemini_tts` in `jetson-hub/voice.py`): Gemini hears the recording, names the language and regional variety, and translates it.
- **What's innovative or technically impressive**:
    - **The family can talk to the car, not just watch it.** One multimodal call fuses live camera frames, another AI's verdicts (NVIDIA Nemotron), an edge model's people count (the UNO Q), a physical sensor (Modulino Thermo) and the location into a short answer, in the language of the question ("Onko autossa kuuma?" is answered in Finnish). It stays silent in the car, so it works even when calling is dangerous.
    - Written to be safe for scared people: it describes only what is visible or measured, never guesses at injuries or intentions, says when the camera is dark or nobody is visible, and reminds them of `112` and the location when it looks serious. After a threat, it never suggests calling the driver.
    - Video understanding and Maps grounding combined into one report a stressed person can act on.
    - One spoken request can chain Search, Maps and actions in the physical car ("find the nearest open pharmacy and text mum I'm stopping there").
    - Audio in, regional-variety translation out, in a single call: no separate speech-to-text, and Portuguese from Portugal is answered as Portugal's, not Brazil's.
    - Privacy by design: Gemini is called only when someone says "Google", turns on the translator, or an alert fires. The contacts can ask the car only for 15 minutes after an alert, never on an ordinary drive.
- **Why Gemini**: it's the one model here that understands video, images and audio directly, answers in any language, and is grounded in live Google Search and Maps data. Without it, the alert would be a location and a clip; with it, it's a plan, and a conversation with the car while help is on the way.

### Best Use of Gemma

- **Model**: Gemma 4 E2B (`gemma4:e2b`) on Ollama, on the Jetson's GPU, with thinking off. Everything it does runs offline in the car.
- **Where** (all in `jetson-hub/voice.py`):
    - `understand`: turns the driver's words into an action (message, light, directions, answer, ask Google) as JSON, with the driver's contacts in the prompt and a fixed JSON schema.
    - `local_answer`: short spoken answers, in the passenger's own language for the back seat.
    - `say_in`: translates the car's replies into the back seat's language.
    - `prepare_llm` / `fetch_llm`: downloads the model over IPv4 and keeps it loaded.
- **Why it matters**: privacy by default. Nothing said in the car goes online unless the speaker says "Google".

### Best use of Arduino UNO Q

The full write-up for the Arduino team, with the architecture, the demo and what we measured, is in [docs/ARDUINO.md](docs/ARDUINO.md).

- The **Arduino UNO Q** with the **Arduino USB Camera** is the car's camera node. It joins the hotspot on its own and runs `unoq-camera/stream.py` as a systemd service (`unoq-camera/safedrive-camera.service`).
- `stream.py` finds the camera (`find_camera`) and serves its own JPEGs at 800×600, 10 fps (`/stream.mjpg`, `/frame.jpg`, `/preview.mjpg`). It reports `/health`, including when the picture is dark, recovers from stuck auto-exposure (`kick_exposure`), and serves a live view with the on-device person boxes, the Modulino Thermo's reading and the hand-sign guide (`unoq-camera/icons/`).
- Its frames feed every AI feature that needs vision: Nemotron's cabin watch and hand signs (`watch_loop` in `jetson-hub/voice.py`), the alert photo and clip (`jetson-hub/vision.py`) and Gemini's incident report and answers after an alert (`describe_clip` and `ask_context` in `jetson-hub/hub.py`).
- A **Modulino Thermo** on the UNO Q's Qwiic connector measures the cabin. Both halves of the board are used: the sketch `unoq-camera/thermo/thermo.ino` runs on the STM32 microcontroller, reads the sensor every 2 s and offers `thermo_read` through the Arduino router (`Arduino_RouterBridge`). On the Linux side, `climate_loop` in `stream.py` calls it over the router's socket and serves `/climate`. `tools/deploy.py unoq-camera` builds and flashes the sketch on the board itself whenever it changes.
- The cabin temperature drives the too hot / too cold warning and banner (`check_cabin` in `jetson-hub/hub.py`, `cabin_warning` in `voice.py`), goes into every incident report, shows on the safeDrive page and on the UNO Q's own live view, and answers "how warm is it in the car?" (`climate_sentence` in `jetson-hub/voice.py`, and the Gemini function `cabin_climate`).
- **Edge AI on the UNO Q: on-device person detection.** Arduino's Edge Impulse model runner (the `ei-models-runner` container from Arduino App Lab, started by `tools/deploy.py`) runs the YOLOX-nano detector from Arduino's model list on the UNO Q, on the camera's own frames. `occupancy_loop` in `stream.py` sends it the newest frame about once a second: about 0.5 s per frame, about 180 MB of RAM, fully offline, while the camera keeps streaming. It serves the people count at `/occupancy`, and the UNO Q's live view draws the boxes ("person 0.86").
- **The UNO Q's LED matrix** shows the on-device count as a digit and a cloud icon while frames go to Nemotron. This is the other direction through the Arduino router: Linux (`matrix_loop` in `stream.py`) calls `matrix_show` on the microcontroller, which draws it (`thermo.ino`). The hub tells the UNO Q whether frames are going out with each poll (`cloud_active` in `jetson-hub/hub.py`).
- **Two tiers of AI**: the UNO Q watches occupancy all the time, on the device; Nemotron in the cloud does the heavy reasoning (threats, closed eyes, phone use, hand signs) while someone might be in the car. Only after the UNO Q has seen an empty cabin for 10 s does the cloud watch slow to one check every 15 s (`cabin_empty` in `jetson-hub/voice.py`). It's fail-open: if the detector misses someone, Nemotron still looks within 15 s, and if the detector is down or the cabin is too dark to tell, nothing changes. The too hot / too cold warning only speaks when the UNO Q has seen someone in the last minute, or can't tell (`occupied` in `jetson-hub/hub.py`). The Gemini agent's `cabin_status` includes the on-device count.
- Gemma 4, Whisper and Piper run on the Jetson; Nemotron runs on Verda.

### Verda Compute Track

- **What we built on Verda**: the cabin watch. A car's camera is watched continuously by NVIDIA Nemotron 3.5 Super VL, served by the Verda Inference API from a dedicated container endpoint (`NEMOTRON_URL` and `VERDA_INFERENCE_KEY` in `jetson-hub/voice.py`).
- **Ambition: what compute made possible**: a 120B-parameter vision-language model (12B active) judging every few seconds whether the driver is in danger, falling asleep or on their phone, and reading passengers' hand signs. That's far beyond the car's Jetson Orin Nano, whose GPU already runs Gemma 4 for the voice assistant. With Verda, a small computer in the car gets a frontier-scale vision model.
- **Fit: why the Inference API**: no servers to run during a hackathon, an OpenAI-compatible API we called with plain HTTP and a JSON schema for structured verdicts, and European hosting: video from inside a private car is processed in Europe, by an open-weight model.
- **Process**, from the git history:
    1. **First version** (`Add a voice assistant with Gemma, Gemini and Nemotron`): bursts of 3 frames from the last 1.5 s, so the model sees movement such as a hand reaching for the wheel, scaled down to 640 px (about 30 KB each) to cut the upload over a phone hotspot. One request at a time, with a JSON-schema verdict.
    2. **Less waste** (`Show a camera-off icon when the camera can't see`): dark or stale frames are never sent, so no GPU time is spent on a covered lens or a parked car at night, and the driver sees when the watch is paused.
    3. **Hand signs** (`Improve hand signs, audio and the camera page`): a `hands` field that makes the model describe the hands across all frames before it names a sign. Voting adapts to the endpoint's speed.
    4. **Robustness**: when the endpoint is busy or fails, the watch retries after 15 s instead of stopping.
    5. **Sharing the hotspot**: once the endpoint answered in about 1 s, the watch sent 55-77 requests a minute and the car's speech broke up. It now sends at most one every 2 s (`WATCH_MIN_INTERVAL_S`) and nothing while the car speaks: about 28 a minute.
    6. **More to watch, same request**: the schema gained `neck`, `eyes` and `phone` fields, answered before the danger level, so choking, closed or covered eyes and phone use cost no extra requests. A **What NVIDIA Nemotron sees** card on the live view shows every verdict and its response time.
    7. **Measured** (`tools/eval_watch.py`): 135 labelled bursts of 27 acted scenes, scored for every prompt version. It showed the new neck field had made threats worse (0 of 15 caught), because Nemotron picked the category before describing the hands. With the hands described first and the alert decided in code, it catches 13 of 15 with no false alarms, at the same 1 s per verdict ([results](#evaluation-results)).
- **Path to scale**: each car sends at most one small request every 2 s, one every 15 s while the UNO Q's on-device detector sees an empty cabin, and nothing while the camera can't see, so load grows linearly with the number of cars on the road. A fleet would move to Verda's auto-scaling containers, with a smaller model on the car's Jetson as an offline fallback. The same endpoint can serve taxis, school buses and delivery fleets, with all cabin video staying in Europe.

### Atech Real World Challenge

Two Atech 14-port boards (ESP32-S3) are the car's body: every input and output in the car goes through them. The firmware is written as Atech projects, and the `atech` tool (`atech==1.0.0a12`) builds, validates and flashes it on the Jetson.

| Module | Port(s) | Used for | Where |
|---|---|---|---|
| `st7735_tft` screen | 1-2 | Menu (check-in, translator), what the car heard and answered, camera-off icon, cabin temperature | `atech-flasher/project.yaml` |
| `microphone` | 3-4 | Driver's voice, streamed to the Jetson over UDP | `project.yaml` (Voice section) |
| `button` | 5 | Voice button (push to talk, green while listening) | `project.yaml` |
| `button` | 6 | SOS (hold 2 s) | `project.yaml` (Scenario 2) |
| `neopixel` grid | 7 | Cabin light, switched by voice | `project.yaml` |
| `icm40608` IMU | 9 | Crash and flip detection | `project.yaml` (Accident detection section) |
| `speaker` | 10-11 | Spoken answers, translations, hand signs | `project.yaml` |
| `rotary_encoder` knob | 13-14 | Menu navigation, cancelling the accident countdown | `project.yaml` |
| Board 2: `button`, `speaker`, `microphone` | 9, 3-4, 13-14 | Back-seat intercom and translator | `atech-flasher/passenger.yaml` |

- **Build and flash**: `atech-flasher/runner.py` runs `atech validate`, builds both projects with PlatformIO, tells the two boards apart on USB, flashes each and streams their logs.
- **AI and external APIs** (bonus): the boards talk MQTT to `jetson-hub/`, which connects them to Gemma, Gemini, Nemotron, Google Maps, OpenStreetMap and Telegram.
- **Real problem, real users**: the driver can call for help without looking at a phone, the driver's contacts hear about a crash even if nobody in the car can answer, and passengers can talk to the driver without distracting them.

## How fast it is

Measured on the hardware on 14 November 2026, in the middle of Junction 2026. The car reached the internet through a phone hotspot on a crowded venue network, and the cloud endpoints were shared with many other teams testing at the same time. Expect every online step to be faster on a normal connection and a dedicated endpoint.

| Step | Where it runs | Time |
|---|---|---|
| Understanding a voice command (Gemma 4 E2B) | Jetson, offline | 1.7-2.0 s |
| Translating one turn, either direction (Gemini) | Cloud | 2.7-3.5 s |
| "Ask Google" with a web search (Gemini agent) | Cloud | about 10 s |
| AI incident report after an alert (Gemini, Maps, OpenStreetMap) | Cloud | about 10-15 s |
| Answering a contact's question after an alert ("ask the car", Gemini) | Cloud | 3.4-6.8 s in our tests on 15 November |
| Counting the people in the cabin (Edge Impulse YOLOX-nano) | UNO Q, offline | about 0.5 s per frame |
| One cabin-watch verdict (Nemotron on Verda) | Cloud | about 1 s when the shared endpoint isn't busy, longer when it is |

## Next steps

- **Custom hardware with Arduino and Atech.dev**: purpose-built, encased boards for the driver's console and the back seat, a discreet SOS button that fits anywhere, and the Jetson and UNO Q hidden in the glove compartment.
- **Connect to the car**: read the car's own sensors over CAN bus or OBD-II (airbag deployment, speed, seatbelts, doors) for more reliable crash detection, and control the media system, so the radio turns down when the car speaks or when a passenger signs "volume down". Use the car's own speakers and microphones.
- **Call emergency services**: hand the incident report and location to Europe's eCall system or the local emergency centre, with a confirmation step so false alarms never reach them.
- **Watch the driver more closely**: the car already warns about closed or covered eyes and a phone in hand. Next: catch drowsiness before the eyes close (yawning, nodding off), eyes off the road for too long, and use the car's speed so a driver resting in a parked car isn't warned.
- **Protect who's left in the car**: alert when a child or pet is left in a parked car that's getting hot.
- **Faster and more accurate AI**: work with Google, NVIDIA, Verda and Wendy to cut latency, run a smaller vision model on the Jetson so the cabin watch also works offline, and tune the models on real cabin footage.
- **Partner with a carmaker**: offer safeDrive as a factory option or a dealer-fitted accessory, built into the car's screen and app.
- **More drivers**: taxi, ride-hailing and delivery fleets, school transport, new and older drivers, and insurers who reward safer driving.

## Design choices and known limits

- **A person decides to call 112.** Nothing calls emergency services automatically: the alert and incident report give the driver's contacts what they need to make the call, so a false alarm never sends an ambulance.
- The camera has no infrared: in a dark cabin, Nemotron isn't asked, the driver's screen shows a crossed-out camera icon, and the UNO Q's person detection reports "can't tell" (never "empty").
- "Ask Google", the translator, the incident report and "ask the car" need the internet; with no `gemini_api_key`, the rest still works.
- The safeDrive page can't keep the iPhone's screen on by itself (Auto-Lock must be set to Never), and it can only open Google Maps or Waze from a tap.

## Thanks

safeDrive was built in one weekend at [Junction 2026](https://2026.hackjunction.com/). Thank you to the Junction organisers and volunteers, and to the partners whose hardware, models and help made it possible: **Atech** for the modules and the firmware platform, **Arduino** for the UNO Q, the camera and the Modulinos, **NVIDIA** and **Verda** for Nemotron and the GPUs that run it, **Google DeepMind** for Gemini and Gemma, and **Wendy** for the Jetson Orin Nano and WendyOS. Last but not least, thank you to the judges for their time, and to everyone who tried safeDrive, shared their feedback and voted for us during the hackathon.

## Licence

[MIT](LICENSE), © 2026 team madpiece.
