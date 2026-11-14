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
3. **Phone location**: install OwnTracks and set Mode MQTT, Host `172.20.10.2`, Port `1883`, TLS off, Authentication off, location access _Always_, monitoring _Move_.
4. **Telegram**: each parent opens their bot and taps **Start**. The bot replies "✅ SafeDrive connected".

## Deploy

```sh
python3 tools/deploy.py jetson-hub      # ~2 s once cached
python3 tools/deploy.py atech-flasher   # ~1 min: build, flash, wait for the board's MQTT connection
```

`deploy.py` runs the app's `make_compose.py`, which generates `compose.yml` with the code and secrets embedded. Then it runs `wendy run --detach` and follows the logs until the app is up. The device defaults to `wendyos-hackathon-device-4.local`; set `WENDY_DEVICE` to override it. Each run's `compose.yml` is generated and holds secrets, so it isn't committed.

## SOS button (port 6)

- Hold 2 s: two amber flashes, and the alert goes out to every linked chat.
- Red for 3 s: the hub reported a problem (no chats linked, every send failed, or no recent GPS fix, in which case the alert went out without a location).
- The white light while it's pressed is built into the V2.0 button, and firmware can't turn it off.

The port 3 button toggles the light grid.
