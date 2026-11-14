# safeDrive features

How each feature behaves, in detail. The overview is in the [README](../README.md); setup, deploy and configuration are in [SETUP.md](SETUP.md).

## Safety

### SOS button (port 6)

- Hold 2 s: two amber flashes, and the alert goes out to every linked chat at once, with a live location. A photo from the car's camera follows, then a clip from 10 s before to 5 s after the press (see Camera). If the last location is over 1 minute old, the hub asks the phone for a new one and the live location moves there when it arrives. With no location at all, the live location follows as soon as the phone answers (up to 60 s).
- If the phone hasn't sent a position for over 30 minutes (live location sharing stopped, OwnTracks asleep), SOS, accident and threat alerts still send the last known one, up to 24 h old (`LAST_KNOWN_MAX_AGE`), marked "⚠️ Last known position, 45 min ago: the car may have moved". The incident report says the same. The live location moves as soon as the phone answers. Check-ins still only use positions up to 30 minutes old.
- Otherwise the light stays off, so the button stays discreet. It never turns red, even when the hub reports a problem (no chats linked, every send failed, or no recent GPS fix). The problem only shows up in the board's log.
- The white light while it's pressed is built into the V2.0 button, and firmware can't turn it off.

### Accident detection (orientation sensor on port 9)

- **Crash**: a jolt of more than 2.5 g in total (`CRASH_G` in `atech-flasher/project.yaml`). The sensor tops out at 2 g per axis, so only a hard knock gets there.
- **Flip**: the board turns more than 75° away from how it sat at power-on (`FLIP_DEG`; a car on its side is about 90°) and stays like that, still, for 3 s. Not while the voice button is in use or for 10 s after (`FLIP_VOICE_GRACE_MS`): picking the board up to talk into it looks just like a roll. Power the board on while it sits in its mounted position: that's the "upright" it compares against.
- Either one shows **POS ACCIDENT DETECTED / ARE YOU OK?** with a 10 s countdown, and the knob ring turns red. Press the knob for **I'm ok** to cancel.
- If nobody presses, every linked chat gets "possible accident detected" with the live location, plus: call the driver, and if they don't answer, call emergency services (`EMERGENCY_NUMBER`, default `112`). The location works the same as for the SOS button. The screen shows **Alert sent**, **Failed** with the reason, or **No reply**.
- If the hub can't be reached when the countdown ends, the alert waits and goes out as soon as it can. Until then, a knob press still cancels it.
- The alert also gets a photo from the car's camera and a clip from 15 s before the impact through the countdown (see Camera).
- Nothing calls emergency services automatically. The message asks the people in the chats to do it.

### Cabin watch (NVIDIA Nemotron 3.5 Super VL)

- `voice.py` sends Nemotron 3.5 Super VL (Verda endpoint, `VERDA_INFERENCE_KEY` in `.env` or `verda_inference_key` in `jetson-hub/secrets.json`) a burst of 3 camera frames from the last 1.5 s, downscaled to 640 px, one request at a time, as fast as the endpoint answers (a few seconds when it isn't busy).
- It judges two things. **Danger to the driver**: only a weapon, hitting or restraining the driver, or grabbing the wheel, gear lever or handbrake counts as danger. That goes to `hub.py` (`car/vision/threat`), which alerts every linked chat silently like SOS, with photo and clip; nothing shows or sounds in the car. A blocked view or covered eyes is only logged. **Hand signs** (see below): spoken to the driver once Nemotron reports the same sign in 2 of its last 3 answers (or in one answer when Nemotron took 5 s or more, so a busy endpoint doesn't make someone hold a sign for a minute), never acted on.
- Every verdict is published, retained, on `car/vision/nemotron`; the Gemini agent can read the latest one ("is everything OK in the back?").
- If the camera can't see (the picture is almost uniformly black: lid closed, lens blocked, or a dark cabin at night, since the camera has no infrared) or sends nothing, Nemotron isn't asked, and the board's screen shows a crossed-out camera icon in the top-right corner until the picture is back (`car/vision/camera`, retained: `ok`, `dark` or `off`).

#### Hand signs to try

Make the sign towards the camera and hold it still for 2-3 seconds: the car speaks it once Nemotron reports it in 2 of its last 3 answers (about one a second), and not again until it's gone from the last 3.

| Sign | How to make it | The car says |
|---|---|---|
| 👍 All good | Thumbs up | "Hand sign: All good back here." |
| ⏸️ Need a break | Both hands make a T (time-out) in front of you: one flat hand horizontal, the other upright and touching it | "Hand sign: Can we stop for a break soon?" |
| 🔉 Volume down | Flat hand, palm down, push downward | "Hand sign: Could you turn the volume down, please?" |
| 🔊 Volume up | Flat hand, palm up, lift upward | "Hand sign: Could you turn the volume up, please?" |
| ✋ Slower | Flat hand, palm towards the camera, fingers up, held still like a stop sign | "Hand sign: Could you slow down, please?" |
| 🔄 Faster | Roll your hand forward in circles, or circle a finger | "Hand sign: Could you go a bit faster?" |
| 🙏 Thank you | Flat hand, fingers together, fingertips at your chin, then move it forward and down | "Hand sign: Thank you!" |

- Waving, talking with your hands, touching your hair, glasses, ears or nose and resting your chin on a curled hand don't count. Slow, held signs work best: fast movement blurs on the camera.
- Anyone the camera sees can sign, the driver included (`gesture_from: "any"`, the default, for demos at a table). In a real car, set `"gesture_from": "back"` in `jetson-hub/secrets.json`: then only people in the back seat count, and the car says "From the back seat: ..." instead of "Hand sign: ...".
- The car stays quiet while you're talking to it. The hub log shows what Nemotron saw for each verdict, including a `hands=` description of the hands.

### Camera (Arduino UNO Q + Arduino USB Camera)

- The camera goes into a powered USB-C hub on the Uno Q's only USB-C port. The Uno Q joins the hotspot by itself and serves the camera at `http://172.20.10.4:8090/` (live view), `/frame.jpg` (newest frame) and `/stream.mjpg`.
- On the Jetson, `vision.py` (part of `car-hub`) keeps the last 40 s of frames in RAM only. With an SOS, accident or threat alert, `hub.py` sends every linked chat a photo, an H.264 clip (960 px wide, about 15 KB per second of a still scene) and an AI incident report. If the camera is off, the alert and incident report go out without the photo and clip.
- The camera's auto exposure can get stuck and deliver black frames with the lens open. When frames stay near-black for 10 s, the streamer switches to manual exposure and back to auto, at most once a minute, and logs it (`journalctl -u safedrive-camera` on the Uno Q). `/health` shows `"dark": true` while frames are near-black: a covered lens, night, or a stuck camera.

### Incident report

- About 10–15 s after the alert (once the clip is ready, so it can come just before or after the video), `hub.py` sends a short plain-text summary from Gemini: what the clip shows inside the cabin, how many people are visible, the street address or nearest landmark, the nearest emergency department (from Google Maps), and one or two sentences to read to `112` (for a threat, it warns not to call the driver first).
- The message says it is AI-generated and can be wrong, and tells the driver's contacts to check the clip. It describes only visible movement in the cabin and never diagnoses injuries; if the video is dark, blurry or missing, it says so instead of guessing.
- To keep it quick, three lookups run in parallel. As soon as the alert fires, OpenStreetMap (Nominatim) gives the exact street address at the phone's position (about 0.5 s) and Gemini with Google Maps finds the nearest emergency department. The third, a Gemini call with no tools that answers in JSON, describes the clip as soon as it's ready. Thinking is turned off for both Gemini calls. Gemini's own address is used only if OpenStreetMap has none: it names a nearby place, and often a different one each time.
- The hub log shows which Google Maps calls Gemini made and the places Maps returned (`Maps sources: ...`).
- Only when an SOS, accident or threat alert fires do the clip, the alert trigger, the cabin monitor's verdicts from that window and the car's location go to Gemini (and the coordinates to Google Maps and OpenStreetMap).

## Driver

### Voice assistant (button on port 5, mic on ports 3-4, speaker on ports 10-11)

- Hold the button while you talk, or tap it once to start and tap again when you're done. It chimes and stays green while it listens. The button is the wake word: "hey car" is optional. Say what you want, for example:
    - "Hey car, send a message to mum, I'm running 5 min late" (also dad, or both): a Telegram text from that bot, "💬 From the driver: ...". No location is attached.
      A message goes to everyone only if you say "both", "everyone", "all", "family" or "parents" or name every contact; if the model can't tell who, the car asks "Who should I send it to?" instead of guessing, and for 30 s your next press can be just the name ("Dad", "Send it to mom", "Both"; in that answer, sound-alikes such as "that" or "dead" count as dad and "mum" or "ma" as mom). Speech recognition also gets the contact names as hints. Tip: wait for the chime before speaking, and let go half a second after you finish. Common mishearings are fixed first ("send dead" → dad, "mum" → mom).
    - "Hey car, ask Google what's the weather for today", or "find the nearest open pharmacy and text mum I'm stopping there": a Gemini agent with Google Search, Google Maps (at the phone's last location) and the car's own functions (send a message, the light, the cabin camera's latest verdict). It chains them and the answer is read out, kept to one short sentence (two if really needed); it says "One moment" first.
    - "Hey car, directions to Kamppi" (also "take me to ...", "navigate to ..."): a **Go to Kamppi** button on the safeDrive page on the phone (see safeDrive page). Without "Google", no AI service sees the destination: Google Maps looks it up when you tap Go. With "Google" ("ask Google for directions to the nearest open pharmacy"), Gemini finds the exact place with Maps first and puts that on the button.
    - "Hey car, turn on the light" / "turn off the light": the light grid on port 7.
    - Anything else, like a quick fact or a joke, gets a short answer from the local model.
- Listening stops when you let go (after holding it for more than 0.6 s) or on the second tap (chime, light off), or after 20 s. If it heard no speech at all, it says so and does nothing.
- Mic audio goes to the Jetson over UDP (port 1885), not MQTT, so a busy network can never freeze the board's loop or the SOS button. A lost packet is a short gap in the audio.
- The screen shows what it heard and the answer, and the speaker reads it out. Pressing the button again interrupts the answer and starts a new question.
- Nothing you say goes online unless you say "Google" (or "Gemini"): then exactly what you said goes to Gemini, with the car's location. Anything else is handled in the car; "what's the weather?" without "Google" gets "I can't check that offline. Say 'ask Google' to look it up." Messages to contacts still need the internet for Telegram, but no AI service sees them.
- Everything runs on the Jetson except "ask Google": speech-to-text (faster-whisper `small.en`, CPU), the local LLM that decides what to do (Gemma 4 E2B, `gemma4:e2b` on Ollama, GPU, thinking off) and the voice (Piper `en_US-lessac-medium`). While the LLM is still loading, the common phrasings above still work.
- An accident countdown stops listening, silences the speaker and drops the half-finished request.

### Check-in menu (screen on ports 1-2, knob on ports 13-14)

- Turn the knob to move through the menu and press it to select.
- **I'm ok** opens: Send to mom, Send to dad, Send to both, Go back.
- Picking a recipient sends "I've arrived and everything is ok." plus the phone's location right away, using the last location from up to 30 minutes ago. If it's over 1 minute old, the hub also asks the phone for a new one and sends a second pin if the phone answers from more than 100 m away to that bot (`mom` and `dad` in `jetson-hub/secrets.json`). The screen then shows **Sent** (green), **Sent** without location (orange, no recent GPS fix) or **Failed** with the reason (red), and goes back to the menu. You can press the knob to close it sooner.
- **Go back** returns to the main menu.

### safeDrive page (phone in the holder)

- On the driver's phone, open `http://172.20.10.2:8092/` in Safari (the phone is the hotspot, so it always reaches the Jetson) and leave the page on screen. Add it to the home screen to open it faster.
- Ask the car for directions and the page shows the place with a big **Go to ... →** button. One tap opens Google Maps with the route, ready to start. **Open in Waze instead** does the same in Waze. Both open the website if the app isn't installed.
- Nothing opens by itself: iOS only lets a page open another app from a tap.
- When the car corrected a place name it misheard, the card shows what it heard ("Heard "campy"" under Kamppi).
- The page also says when the cabin camera can't see. The destination is retained on `car/nav/destination`, so a page opened later still shows it; the button stays for 6 hours.

## Back seat

### Back-seat intercom (board 2: button on port 9, speaker on ports 3-4, mic on ports 13-14)

- Works like the driver's port 5 button: hold it while you talk, or tap to start and tap again when you're done. Green while it listens, blue while it thinks, a red flash and a low tone if the hub can't be reached. The answer plays on the back-seat speaker only.
- In translator mode the button only translates (see Translator).
- **Questions only.** The back seat gets local answers ("how far is the moon?") and, when it says "Google", the Gemini agent with Search and Maps ("ask Google when the Ateneum closes"). It can never send a message to the driver's contacts on Telegram, switch the light or put directions on the driver's phone: `voice.py` refuses those ("From the back seat I can only answer questions. Ask the driver for that."), and Gemini gets none of those functions for a back-seat request.
- Nothing from the back seat shows on the driver's screen. The driver and the back seat can ask at the same time; each board keeps its own request.
- Topics: `car/passenger/voice/start`, `/end` (board to hub), `/pcm`, `/stop` (hub to board), `car/passenger/status` (retained online flag). Mic audio goes to the same UDP port 1885 as the driver's; the session id tells them apart.

#### The back seat's language

- The back-seat button understands any language: its speech-to-text is the multilingual Whisper `small` (the driver's stays `small.en`, the most accurate for English), loaded in the background after start (first start downloads about 480 MB; until then the back seat is English-only).
- Without "Google", everything stays in the car: Gemma answers in the passenger's language and a local Piper voice reads it (`pt_PT-tugão-medium` for Portuguese, which is Portugal's; Spanish, French, German, Italian, Finnish, Swedish and about 20 more; `PIPER_VOICES` in `voice.py`). Each voice downloads on first use (about 63 MB); Portuguese is fetched at start (`prefetch_langs`). A language with no local voice gets English.
- With "Google" (or "Gemini") in the question, in any language ("Pergunta ao Google ..."), Gemini answers in the passenger's language and variety and its own voice reads it. The answer is read by the local voice of their variety when there is one (`pt_PT-tugão` for Portugal): Gemini's own voice drifts to a Brazilian accent and reads any accent direction aloud, so it's only used for varieties with no local voice. Whisper only names the language, and Portuguese is taken to be Portugal's.

### Translator (driver's screen menu → Translator)

- For a back-seat passenger who doesn't speak English. On the driver's screen, turn the knob to **Translator** and press it: the screen shows **Translator ON**, the back seat hears "Translator on." and their button flashes cyan until they press it. Press the knob again to turn it off: the back seat hears "Translator off." It's off after the board restarts.
- **Back seat → driver:** the passenger presses their button (port 9) and speaks their own language. Gemini (`gemini-3.8-flash`) hears the recording, names the language and its regional variety from the accent, words and grammar (Portuguese (Portugal), not just Portuguese) and translates it in one call (about 3-4 s); the driver hears it in English on their speaker, and the screen shows it under the language's name. The back-seat button chimes and goes dark when it's passed on.
- **Driver → back seat:** the driver presses the voice button (port 5) and speaks English. Gemini translates it into the exact variety the back seat last spoke, with that country's words, grammar and spelling (Portugal: "estamos a chegar", "autocarro", "casa de banho", not the Brazilian forms), and it's spoken on the back-seat speaker by the local Piper voice of that exact variety (`pt_PT-tugão` for Portugal, `pt_BR-faber` for Brazil, `es_ES`/`es_MX`, ...; `VARIETY_VOICES` in `voice.py`), or by Gemini TTS (`gemini-3.8-flash-tts`, voice Kore) with the variety's language code when there's no local voice. If the back seat hasn't spoken yet, the car says so: it doesn't know their language.
- The flow: the driver turns it on, and the screen shows "Waiting for the back seat to speak first". The passenger speaks; the screen then shows their language ("Back seat: Spanish", retained on `car/translator/lang`) and "Voice button: reply". The language is learned again each time the translator is turned on.
- Pressing the knob cancels it at any point and goes back to the menu: anything still being translated or played is dropped, on both boards.
- The driver board owns the mode: it sends it with every voice press (`"tr"` in `car/atech/voice/start`), so the hub can't mistake a reply for a voice command even if it missed the menu change.
- While it's on, every press on either board is only translated: no voice commands, no messages to the driver's contacts, no "ask Google". Turn it off for those.
- Privacy: only in this mode does recorded speech go to Google (Gemini), from both boards. The driver turns it on.
