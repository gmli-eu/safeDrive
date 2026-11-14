# Generates compose.yml: Mosquitto + hub + voice assistant (voice.py) + Ollama (its local LLM)
# + camera buffer (vision.py) + the phone page (dash.py). hub.py, voice.py, vision.py and dash.py are embedded as base64 (no build needed; Wendy skips bind mounts and
# rejects newlines in env vars). Secrets come from secrets.json.
import base64, json, pathlib, zlib
here = pathlib.Path(__file__).parent
secrets = json.loads((here / "secrets.json").read_text()) if (here / "secrets.json").exists() else {}
# zlib-compressed: Wendy caps each env var at 32 KB.
b64z = lambda path: base64.b64encode(zlib.compress(path.read_bytes(), 9)).decode()
hub_b64 = b64z(here / "hub.py")
# paho-mqtt is installed once into the data volume (pure Python, so it runs from there)
# and reused on every start, instead of being reinstalled over the hotspot each time.
boot = ("import os, subprocess, sys, base64, shutil; "
        "S = '/data/site-paho-mqtt-2.1.0'; "
        "shutil.rmtree(S + '.new', True); "
        "os.path.isdir(S) or (subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '--root-user-action=ignore', "
        "'--disable-pip-version-check', '--cache-dir', '/data/pip', '--target', S + '.new', 'paho-mqtt==2.1.0'], check=True) "
        "and os.rename(S + '.new', S)); "
        "sys.path.insert(0, S); "
        "import zlib; exec(zlib.decompress(base64.b64decode(os.environ['HUB_B64'])))")
bots = secrets.get("telegram_bots", [])
if not (secrets.get("gemini_api_key") and secrets.get("verda_inference_key")):
    # Fall back to GEMINI_API_KEY / VERDA_INFERENCE_KEY in the repo's .env (ignored by git, like secrets.json).
    env_file = here.parent / ".env"
    for line in env_file.read_text().splitlines() if env_file.exists() else []:
        k, _, v = line.partition("=")
        if k.strip() in ("GEMINI_API_KEY", "VERDA_INFERENCE_KEY") and v.strip() and not secrets.get(k.strip().lower()):
            secrets[k.strip().lower()] = v.strip().strip('"').strip("'")
tz = secrets.get("tz", "Europe/Helsinki")
env = {"HUB_B64": hub_b64, "MQTT_HOST": "127.0.0.1", "LIVE_PERIOD": "900", "STATE_DIR": "/data", "TZ": tz,
       "TELEGRAM_BOTS": json.dumps(bots), "GEMINI_API_KEY": secrets.get("gemini_api_key", "")}
if secrets.get("gemini_model"): env["GEMINI_MODEL"] = secrets["gemini_model"]
# The Uno Q serves the cabin climate (Modulino Thermo) next to the camera stream.
camera_url = secrets.get("camera_url", "http://172.20.10.4:8090/stream.mjpg")
env["CLIMATE_URL"] = secrets.get("climate_url", camera_url.rsplit("/", 1)[0] + "/climate")
for key in ("hot_car_c", "cold_car_c", "cabin_hold_s"):   # optional: lower the limits for a demo
    if key in secrets: env[key.upper()] = str(secrets[key])
# voice.py is too big for one env var even compressed: split into VOICE_B64_0, _1, ... (joined at start).
voice_b64 = b64z(here / "voice.py")
VOICE_PART = 30000
voice_parts = {f"VOICE_B64_{i}": voice_b64[o:o + VOICE_PART] for i, o in enumerate(range(0, len(voice_b64), VOICE_PART))}
voice_env = {**voice_parts, "MQTT_HOST": "127.0.0.1", "TZ": tz,
             "OLLAMA_URL": "http://127.0.0.1:11434", "DATA_DIR": "/data", "HUB_STATE": "/hub/telegram.json",
             "CONTACTS": json.dumps([b["name"] for b in bots]),   # names only: Telegram stays in the hub
             "GEMINI_API_KEY": secrets.get("gemini_api_key", ""),
             "VERDA_INFERENCE_KEY": secrets.get("verda_inference_key", ""),   # Nemotron 3.5 Super VL
             "VISION_URL": "http://127.0.0.1:8091",
             "PLACES_B64": b64z(here / "places-helsinki.txt")}   # fallback place names until the area's list is fetched
for key in ("llm_model", "whisper_model", "piper_voice", "gemini_model", "gesture_from", "voice_drive", "voice_peak",
            "translate_model", "tts_model", "tts_voice", "whisper_multi_model", "prefetch_langs", "piper_voices"):   # optional overrides
    if secrets.get(key):
        v = secrets[key]
        voice_env[key.upper()] = json.dumps(v) if isinstance(v, dict) else ",".join(v) if isinstance(v, list) else str(v)
vision_env = {"VISION_B64": b64z(here / "vision.py"), "DATA_DIR": "/data", "TZ": tz,
              "CAMERA_URL": camera_url}   # the Uno Q (unoq-camera/)
dash_env = {"DASH_B64": b64z(here / "dash.py"), "MQTT_HOST": "127.0.0.1", "DASH_PORT": "8092", "TZ": tz}
compose = f"""services:
  mosquitto:
    image: eclipse-mosquitto:2.0.20
    command: ["mosquitto", "-c", "/mosquitto-no-auth.conf"]
    network_mode: host
    restart: always
  hub:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", {json.dumps(boot)}]
    network_mode: host
    restart: always
    depends_on: [mosquitto]
    volumes:
      - hub-data:/data
    environment:
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in env.items()) + f"""  ollama:
    image: ollama/ollama:0.40.15
    network_mode: host
    restart: always
    volumes:
      - ollama-models:/root/.ollama
    environment:
      OLLAMA_HOST: "127.0.0.1:11434"
      OLLAMA_KEEP_ALIVE: "-1"
      OLLAMA_MAX_LOADED_MODELS: "1"
      OLLAMA_CONTEXT_LENGTH: "4096"
  voice:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", "import os, base64, zlib; exec(zlib.decompress(base64.b64decode(''.join(os.environ['VOICE_B64_%d' % i] for i in range({len(voice_parts)})))))"]
    network_mode: host
    restart: always
    depends_on: [mosquitto]   # not ollama: voice.py waits for it, and redeploying voice mustn't restart a model download
    volumes:
      - voice-data:/data
      - ollama-models:/ollama   # voice.py downloads the LLM into Ollama's store itself (IPv4)
      - hub-data:/hub
    environment:
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in voice_env.items()) + """  vision:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", "import os, base64, zlib; exec(zlib.decompress(base64.b64decode(os.environ['VISION_B64'])))"]
    network_mode: host
    restart: always
    volumes:
      - vision-data:/data
    environment:
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in vision_env.items()) + """  dash:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", "import os, base64, zlib; exec(zlib.decompress(base64.b64decode(os.environ['DASH_B64'])))"]
    network_mode: host
    restart: always
    depends_on: [mosquitto]
    volumes:
      - dash-data:/data
    environment:
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in dash_env.items()) + """volumes:
  hub-data:
  voice-data:
  vision-data:
  dash-data:
  ollama-models:
"""
assert "$" not in compose
(here / "compose.yml").write_text(compose)
print("compose.yml written", f"({len(bots)} telegram bots, Gemini {'on' if voice_env['GEMINI_API_KEY'] else 'off: no gemini_api_key'})")
