# SafeDrive

Hardware for safer driving.

```
Atech board (ESP32-S3)  --Wi-Fi/MQTT-->  Jetson (WendyOS): Mosquitto + hub.py  --HTTPS-->  Telegram
                                           ^
iPhone (OwnTracks) ---MQTT location--------+
```

| Folder           | Runs on                            | What it does                                                                                                  |
| ---------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `jetson-hub/`    | Jetson (Wendy app `car-hub`)       | Mosquitto broker plus `hub.py`: turns the SOS long press into Telegram alerts with live location              |
| `atech-flasher/` | Jetson (Wendy app `atech-flasher`) | Builds the Atech firmware (`project.yaml`), flashes it to the board over USB, then streams the board's output |
| `tools/`         | dev machine                        | `deploy.py` deploys an app and waits until it's up; `mqtt_peek.py` watches the broker                         |

## Setup

1. **Secrets** (both are ignored by git):
    - `jetson-hub/secrets.json`: copy `secrets.example.json` and add the bot tokens from BotFather.
    - `atech-flasher/secrets.json`: copy `secrets.example.json` and add the Wi-Fi name and password.
2. **Wi-Fi**: the board only sees 2.4 GHz. On an iPhone hotspot, turn on _Maximize Compatibility_. The firmware expects the Jetson at `172.20.10.2` (`MQTT_HOST` in `atech-flasher/project.yaml`).
3. **Phone location**: install OwnTracks and set Mode MQTT, Host `172.20.10.2`, Port `1883`, TLS off, Authentication off, location access _Always_, monitoring _Move_. Turn on remote commands (`cmd`) too: when the hub's last location is over 1 minute old, it asks the phone for a new one.
4. **Telegram**: each parent opens their bot and taps **Start**. The bot replies "✅ SafeDrive connected".
5. **Live location through Telegram** (works better than OwnTracks on an iPhone): on the driver's phone, open one of the SafeDrive bots, tap **Start**, then 📎 → Location → **Share My Live Location** → **Until I turn it off**. The hub takes locations only from chats linked to a bot in `jetson-hub/secrets.json`. Telegram only sends a new position when the phone moves, so while sharing is on, the last position counts for up to 6 hours (`LIVE_SHARE_MAX_AGE`). If sharing stops, it counts for 30 minutes like an OwnTracks fix.

## Deploy

```sh
python3 tools/deploy.py jetson-hub      # ~2 s once cached
python3 tools/deploy.py atech-flasher   # ~1 min: build, flash, wait for the board's MQTT connection
```

`deploy.py` runs the app's `make_compose.py`, which generates `compose.yml` with the code and secrets embedded. Then it runs `wendy run --detach` and follows the logs until the app is up. The device defaults to `wendyos-hackathon-device-4.local`; set `WENDY_DEVICE` to override it. Each run's `compose.yml` is generated and holds secrets, so it isn't committed.

## SOS button (port 6)

- Hold 2 s: two amber flashes, and the alert goes out to every linked chat at once, with a live location. If the last location is over 1 minute old, the hub asks the phone for a new one and the live location moves there when it arrives. With no location at all, the live location follows as soon as the phone answers (up to 60 s).
- Otherwise the light stays off, so the button stays discreet. It never turns red, even when the hub reports a problem (no chats linked, every send failed, or no recent GPS fix). The problem only shows up in the board's log.
- The white light while it's pressed is built into the V2.0 button, and firmware can't turn it off.

The port 3 button toggles the light grid.

## Check-in menu (screen on ports 1-2, knob on ports 13-14)

- Turn the knob to move through the menu and press it to select.
- **I'm ok** opens: Send to mom, Send to dad, Send to both, Go back.
- Picking a recipient sends "I'm ok and I've arrived." plus the phone's location right away, using the last location from up to 30 minutes ago. If it's over 1 minute old, the hub also asks the phone for a new one and sends a second pin if the phone answers from more than 100 m away to that bot (`mom` and `dad` in `jetson-hub/secrets.json`). The screen then shows **Sent** (green), **Sent** without location (orange, no recent GPS fix) or **Failed** with the reason (red), and goes back to the menu. You can press the knob to close it sooner.
- **Go back** returns to the main menu.

## Accident detection (orientation sensor on port 9)

- **Crash**: a jolt of more than 2.5 g in total (`CRASH_G` in `atech-flasher/project.yaml`). The sensor tops out at 2 g per axis, so only a hard knock gets there.
- **Flip**: the board turns more than 60° away from how it sat at power-on (`FLIP_DEG`) and stays like that, still, for 2 s. Power the board on while it sits in its mounted position: that's the "upright" it compares against.
- Either one shows **POS ACCIDENT DETECTED / ARE YOU OK?** with a 10 s countdown, and the knob ring turns red. Press the knob for **I'm ok** to cancel.
- If nobody presses, every linked chat gets "possible accident detected" with the live location, plus: call the driver, and if they don't answer, call emergency services (`EMERGENCY_NUMBER`, default `112`). The location works the same as for the SOS button. The screen shows **Alert sent**, **Failed** with the reason, or **No reply**.
- If the hub can't be reached when the countdown ends, the alert waits and goes out as soon as it can. Until then, a knob press still cancels it.
- Nothing calls emergency services automatically. The message asks the people in the chats to do it.
