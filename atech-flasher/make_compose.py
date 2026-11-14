# Generates compose.yml with project.yaml (zlib-compressed: Wendy caps each env var at 32 KB)
# and runner.py embedded as base64
# (Wendy skips bind mounts and rejects newlines in env vars).
# Wi-Fi credentials come from secrets.json (see secrets.example.json) and are filled
# into project.yaml's @WIFI_SSID@ / @WIFI_PASS@ placeholders here, so they never live in git.
import base64, json, pathlib, sys, zlib
here = pathlib.Path(__file__).parent

secrets_file = here / "secrets.json"
if not secrets_file.exists():
    sys.exit("atech-flasher/secrets.json is missing: copy secrets.example.json and fill in the Wi-Fi details")
secrets = json.loads(secrets_file.read_text())

def c_string(value):
    # Contents of a C string literal: escape backslashes and quotes.
    return value.replace("\\", "\\\\").replace('"', '\\"')

project = (here / "project.yaml").read_text()
for key, placeholder in (("wifi_ssid", "@WIFI_SSID@"), ("wifi_pass", "@WIFI_PASS@")):
    if placeholder not in project:
        sys.exit(f"{placeholder} not found in project.yaml")
    project = project.replace(placeholder, c_string(secrets[key]))

b64 = lambda data: base64.b64encode(data).decode()
compose = f"""services:
  flasher:
    image: python:3.12-slim-bookworm
    command: ["python", "-u", "-c", "import os, base64; exec(base64.b64decode(os.environ['RUNNER_B64']))"]
    network_mode: host
    restart: "no"
    volumes:
      - atech-cache:/cache
    environment:
      RUNNER_B64: "{b64((here / 'runner.py').read_bytes())}"
      PROJECT_B64: "{b64(zlib.compress(project.encode(), 9))}"
volumes:
  atech-cache:
"""
assert "$" not in compose
(here / "compose.yml").write_text(compose)
print("compose.yml written")
