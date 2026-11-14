Add a Gemini-written "incident report" to SafeDrive's emergency alerts. Read AGENTS.md, README.md and HACKATHON.md first, then jetson-hub/hub.py, jetson-hub/vision.py, jetson-hub/make_compose.py and the Gemini code in jetson-hub/voice.py (gemini_call, ask_google).

## Goal

Today an SOS, accident or threat alert (hub.py: send_alert -> send_media) sends every linked Telegram chat a fixed text, a live location, a photo and a clip. A panicking parent has to work out what happened themselves. Add a fifth message: a short report from Gemini that turns the clip and the car's data into something they can act on in seconds. This is the core of our "Best Use of Gemini API" entry, so it has to work reliably on the real hardware.

## What Gemini gets

1. The clip that send_media already fetches from vision.py (`/clip.mp4`, H.264, about 15 KB per second of a still scene, so 0.3–1 MB). Reuse those bytes; don't fetch the clip twice. Send it inline as base64 `video/mp4` (inline_data), not through the Files API.
2. The trigger: SOS (the driver held the emergency button), crash (a jolt over 2.5 g) or flip (turned over 75° and still for 3 s), and for accidents that the driver didn't answer "ARE YOU OK?" within 10 s. For a threat, the Nemotron description that triggered it. The board sends only `{"kind": "crash"|"flip"}`. Don't change the firmware.
3. The cabin monitor's recent verdicts. voice.py publishes every NVIDIA Nemotron verdict on MQTT `car/vision/nemotron` (JSON: level, what, people, instruction, captured_at). hub.py currently returns early on that topic. Keep the verdicts from the last 60 s in a small in-memory deque in that early-return branch (still don't log them), and pass the ones from the clip's time window, with their times relative to the event.
4. The location: recent_fix() (lat, lon, age). Give it to Gemini as text, and as `toolConfig.retrievalConfig.latLng` with the `google_maps` tool, the way voice.py's gemini_call does.
5. The current local time.

## What Gemini returns

Plain text in Telegram, under 900 characters, no markdown (send_message without parse_mode), in this exact structure:

```
📋 AI incident report (Gemini, can be wrong: check the clip)
What the clip shows: <1-2 sentences>
People in the car: <number, or unknown>
Where: <street address or nearest landmark>
Nearest emergency department: <name, distance> (from Google Maps)
If you call 112, say: "<one or two sentences a dispatcher needs: what, where, how many people, whether the driver responds>"
```

For a threat, replace the 112 line with: "Don't call the driver. If you call 112, say: ..."

Prompt rules for Gemini (put them in the system instruction):
- The camera is INSIDE the cabin. It sees the people in the car, not the road. Never describe the road, other cars or the cause of a crash unless it's clearly visible through a window.
- Describe only what is visible: movement, whether the driver moves or responds, people, objects. Never diagnose injuries or guess at medical state ("the driver doesn't move in the last 5 s of the clip" is OK, "the driver is unconscious" is not).
- If the clip is dark, blurry or missing, say so in one line instead of guessing.
- Use Google Maps for the address and the nearest emergency department (hospital with an ER). If Maps returns nothing, write "unknown". Never invent places.
- The family reads this on a phone while scared: short, calm, factual.
- EMERGENCY_NUMBER (default 112) goes into the dispatcher line.

Use grounding tools with plain-text output. Don't combine responseSchema/JSON mode with tools; the API can reject that. Read the model from GEMINI_MODEL (default "gemini-flash-latest"; the `gemini_model` override in secrets.json should apply to the hub too). Set a 60 s timeout. Log the time Gemini took and which grounding tools it used, like ask_google does.

## Where it goes

- In hub.py, after the clip is sent in send_media, call a new `send_report(...)` with the clip bytes, the label (SOS / accident / threat), the accident kind or threat description, and the clip window. send_media needs those extra arguments; pass them through from send_sos / send_accident / send_threat.
- Send the report to the same targets with tg(..., "sendMessage", ...), in parallel like to_all.
- Best effort: it must never delay or break the existing alert, photo, clip or live location. Every failure (no key, no clip, HTTP error, quota, timeout, empty answer) is logged as "<label> report unavailable: ..." and nothing is sent. Don't send an error to the family.
- If there's no clip, still send a report from the trigger, verdicts and location, and say there's no video.

## Config

- The hub container doesn't have the Gemini key yet. In jetson-hub/make_compose.py, add GEMINI_API_KEY (and GEMINI_MODEL when `gemini_model` is set) to the hub's `env`, from the same secrets/.env lookup the voice container uses. Never write a real key into a tracked file.
- Keep make_compose.py's `assert "$" not in compose` passing, and keep HUB_B64 under Wendy's 32 KB env limit (it's zlib-compressed. Check the compressed size after your change).

## Docs

Update README.md: one short subsection under the SOS / accident / camera parts that says what the report contains, that it comes after the clip, that it's AI-generated and can be wrong, and which data leaves the car (clip, trigger, cabin verdicts and location go to Gemini only when an alert fires). Match the README's existing style: short, plain sentences.

## Deploy and test

- Follow AGENTS.md: deploy with `python3 tools/deploy.py jetson-hub` and read the output. Never commit or push.
- `deploy.py` refuses while an alert from the last 2 minutes is still sending its clip. Wait; don't use --force.
- Test: trigger an SOS (hold the port-6 button 2 s) and a crash (knock the board, let the 10 s countdown run out). Check that the report arrives in Telegram after the clip, within about 30 s. Check the hub log for Gemini's timing and the tools it used. Also test with the camera lens covered, and confirm the report says the video is dark instead of inventing things.
- Report back: what you changed, the deploy result, a real report text from the test, and how long Gemini took.
