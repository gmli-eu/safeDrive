# Generates compose.yml: Mosquitto + hub + camera buffer (vision.py). hub.py and vision.py are embedded
# as base64 (no build needed; Wendy skips bind mounts and rejects newlines in env vars). Secrets come from secrets.json.
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
env = {"HUB_B64": hub_b64, "MQTT_HOST": "127.0.0.1", "LIVE_PERIOD": "900", "STATE_DIR": "/data",
       "TELEGRAM_BOTS": json.dumps(secrets.get("telegram_bots", []))}
vision_env = {"VISION_B64": b64z(here / "vision.py"), "DATA_DIR": "/data",
              "CAMERA_URL": secrets.get("camera_url", "http://172.20.10.4:8090/stream.mjpg")}   # the Uno Q (unoq-camera/)
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
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in env.items()) + """  vision:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", "import os, base64, zlib; exec(zlib.decompress(base64.b64decode(os.environ['VISION_B64'])))"]
    network_mode: host
    restart: always
    volumes:
      - vision-data:/data
    environment:
""" + "".join(f"      {k}: {json.dumps(v)}\n" for k, v in vision_env.items()) + """volumes:
  hub-data:
  vision-data:
"""
assert "$" not in compose
(here / "compose.yml").write_text(compose)
print("compose.yml written", f"({len(secrets.get('telegram_bots', []))} telegram bots)")
