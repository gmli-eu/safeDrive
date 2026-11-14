# Deploy an app to the Jetson and follow its logs until it is up, then stop.
#   python3 tools/deploy.py atech-flasher     (waits for "mqtt: connected" from every board it flashed)
#   python3 tools/deploy.py jetson-hub        (waits for the hub's "MQTT connected")
#   python3 tools/deploy.py unoq-camera       (copies the camera streamer to the Uno Q over SSH, and flashes
#                                              the Thermo sketch to its microcontroller when the sketch changed,
#                                              and starts the on-device person detector)
# unoq-camera and jetson-hub first check their web page (PAGE in stream.py / dash.py): UTF-8 and
# a JavaScript syntax check with node. A broken page stops the deploy.
# jetson-hub refuses to deploy while an SOS/accident/threat alert is still being sent: a restart
# kills the photo/clip upload. --check only runs that check; --force deploys anyway.
import re
import json, os, pathlib, shutil, subprocess, sys, threading, time
from datetime import datetime, timezone

DEVICE = os.environ.get("WENDY_DEVICE", "wendyos-hackathon-device-4.local")
APPS = {  # folder -> (wendy app name, line that means "up", log lines worth showing)
    "atech-flasher": ("atech-flasher", "mqtt: connected",
                      ("tools ready", "toolchain changed", "built ", "SUCCESS", "+ atech", "FLASH_OK", "FLASHED", "/dev/tty", "taken to be",
                       "wifi:", "mqtt:", "imu:", "passenger intercom", "responding", "reset reason", "result:")),
    "jetson-hub": ("car-hub", "MQTT connected", ("MQTT connected", "Telegram bot", "waiting for broker", "[vision]", "[dash]")),
}
FAIL = (" failed", "rror:", "Traceback", "No Atech board", "can't tell the boards apart", "look like the same one")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
folder = args[0].strip("/").split("/")[-1]

# Web pages embedded as a PAGE string (the Uno Q's live view, the safeDrive page): check them before
# deploying. A JavaScript syntax error stops the whole page script (once a badge stayed on
# "starting..."), and an escape Python turns into an invalid character crashes every page load.
PAGES = {"unoq-camera": ["stream.py"], "jetson-hub": ["dash.py"]}

def check_pages(folder):
    import ast, tempfile
    node = shutil.which("node")
    if not node: print("page check: node not found, JavaScript not checked", flush=True)
    for name in PAGES.get(folder, []):
        path = pathlib.Path(__file__).resolve().parent.parent / folder / name
        page = None
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "PAGE" for t in n.targets):
                v = n.value.func.value if isinstance(n.value, ast.Call) else n.value   # PAGE = """...""".encode() or """..."""
                page = ast.literal_eval(v)
        if page is None: sys.exit(f"page check: no PAGE in {folder}/{name}")
        try: page.encode()
        except UnicodeEncodeError as e: sys.exit(f"page check: {folder}/{name} PAGE can't be sent as UTF-8 ({e}): "
                                                 "write emoji as the character or \\U0001F321, not as \\ud83c\\udf21")
        if not node: continue
        js = "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f: f.write(js)
        r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
        os.unlink(f.name)
        if r.returncode:
            sys.exit(f"page check: JavaScript error in {folder}/{name} PAGE:\n" + r.stderr.strip()[-800:])
        print(f"page check: {folder}/{name} ok", flush=True)

check_pages(folder)
if folder == "unoq-camera":   # not a Wendy app: copied to the Uno Q over SSH, runs as a systemd service
    import io, tarfile
    root = pathlib.Path(__file__).resolve().parent.parent / folder
    secrets = json.loads((root / "secrets.json").read_text())
    host = os.environ.get("UNOQ_HOST", secrets["host"])
    key = pathlib.Path.home() / ".ssh/unoq"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *(["-i", str(key)] if key.exists() else []),
           f"{secrets.get('ssh_user', 'arduino')}@{host}"]
    unit = (root / "safedrive-camera.service").read_text()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(root / "stream.py", arcname="stream.py")
        if (root / "icons").is_dir(): tar.add(root / "icons", arcname="icons")
        tar.add(root / "thermo" / "thermo.ino", arcname="thermo/thermo.ino")
    subprocess.run(ssh + ["rm -rf ~/safedrive-camera/icons && mkdir -p ~/safedrive-camera && tar -xzf - -C ~/safedrive-camera"],
                   input=buf.getvalue(), check=True)
    # The Thermo sketch runs on the microcontroller: build and flash it on the Uno Q itself (arduino-cli
    # and the SWD flasher are there), only when it changed since the last flash (~1 min).
    libs = "Arduino_Modulino Arduino_RouterBridge"
    r = subprocess.run(ssh + [
        "cd ~/safedrive-camera/thermo && h=$(sha256sum thermo.ino | cut -c1-16) && "
        "if [ \"$(cat .flashed 2>/dev/null)\" = \"$h\" ]; then echo 'thermo sketch unchanged'; else "
        f"for l in {libs}; do arduino-cli lib list | grep -q \"^$l \" || {{ arduino-cli lib update-index >/dev/null && "
        "arduino-cli lib install $l >/dev/null; }; done && "
        "arduino-cli compile -b arduino:zephyr:unoq . >/tmp/thermo.log 2>&1 && "
        "arduino-cli upload -b arduino:zephyr:unoq . >>/tmp/thermo.log 2>&1 && echo \"$h\" > .flashed && "
        "echo 'thermo sketch flashed' || { grep -i -m5 error /tmp/thermo.log || tail -5 /tmp/thermo.log; exit 1; }; fi"],
        capture_output=True, text=True)
    print(r.stdout.strip(), r.stderr.strip()[-500:], flush=True)
    if r.returncode: sys.exit("thermo sketch build or flash failed")
    # On-device person detection: Arduino's Edge Impulse model runner (from Arduino App Lab, already on
    # the board) serving YOLOX-nano on 127.0.0.1:1340. Docker restarts it with the board.
    runner = ("ghcr.io/arduino/app-bricks/ei-models-runner:0.5.0 --model-file /models/ootb/ei/yolo-x-nano.eim "
              "--run-http-server 1337 --dont-print-predictions")
    r = subprocess.run(ssh + [
        "docker inspect -f '{{.State.Running}}' safedrive-people 2>/dev/null | grep -q true && echo 'person detector running' || "
        "{ docker rm -f safedrive-people >/dev/null 2>&1; docker run -d --name safedrive-people --restart unless-stopped "
        "--device /dev/dri:/dev/dri --group-add render -p 127.0.0.1:1340:1337 " + runner + " >/dev/null && "
        "echo 'person detector started'; }"], capture_output=True, text=True)
    print(r.stdout.strip(), r.stderr.strip()[-300:], flush=True)
    if r.returncode: sys.exit("person detector failed to start")
    # sudo reads the password from the first line of stdin; the rest is the unit file.
    r = subprocess.run(ssh + ["sudo -S -p '' sh -c 'cat > /etc/systemd/system/safedrive-camera.service && "
                              "systemctl daemon-reload && systemctl enable -q safedrive-camera && "
                              "systemctl restart safedrive-camera'"],
                       input=(secrets["ssh_password"] + "\n" + unit).encode(), capture_output=True)
    if r.returncode: sys.exit("install failed: " + r.stderr.decode()[-500:])
    print("deployed safedrive-camera to", host, flush=True)
    import urllib.request
    t0 = time.time(); health = None
    while time.time() - t0 < 30:
        time.sleep(2)
        try: health = json.load(urllib.request.urlopen(f"http://{host}:8090/health", timeout=3))
        except Exception: continue
        if health.get("last_frame_age_s") is not None and health["last_frame_age_s"] < 2 and health["fps"]:
            print("UP", health, f"\nview: http://{host}:8090/")
            for _ in range(8):   # the first reading takes up to 2 s
                climate = json.load(urllib.request.urlopen(f"http://{host}:8090/climate", timeout=3))
                if climate["celsius"] is not None: break
                time.sleep(1)
            print("climate:", climate)
            if climate["celsius"] is None: sys.exit("FAILED: no reading from the Modulino Thermo")
            for _ in range(30):   # the detector needs a few seconds after a fresh start
                occ = json.load(urllib.request.urlopen(f"http://{host}:8090/occupancy", timeout=3))
                if occ["people"] is not None or occ["error"] == "picture too dark to tell": break
                time.sleep(1)
            print("occupancy:", {k: occ[k] for k in ("people", "confidence", "ms", "error")})
            sys.exit(0 if occ["people"] is not None or occ["error"] == "picture too dark to tell"
                     else "FAILED: no answer from the on-device person detector")
    print("FAILED: no frames within 30 s", health)
    subprocess.run(ssh + ["journalctl -u safedrive-camera -n 20 --no-pager"])
    sys.exit(1)
app, ready, show = APPS[folder]
wendy = shutil.which("wendy") or str(pathlib.Path.home() / ".local/bin/wendy")
root = pathlib.Path(__file__).resolve().parent.parent / folder
timeout = float(args[1]) if len(args) > 1 else 600
def alert_in_flight():
    # The newest SOS/accident/threat alert in the hub log from the last ALERT_WINDOW s that hasn't
    # finished sending its photo, clip and Gemini incident report yet ("media done") (send_media in hub.py), or None.
    ALERT_WINDOW = 120
    r = subprocess.run([wendy, "device", "logs", app, "--device", DEVICE, "--no-follow", "--tail", "400"],
                       capture_output=True, text=True, timeout=60)
    start = re.compile(r"MQTT car/atech/accident |MQTT car/atech/button .*'sos'|MQTT car/vision/threat .*'high'"
                       r"|\b(SOS|accident|threat) (text|location) to")
    done = re.compile(r"\b(SOS|accident|threat) (media done|failed)|threat ignored")
    busy = None
    for line in r.stdout.splitlines():
        try: rec = json.loads(line)
        except Exception: continue
        ts = rec.get("timestamp")
        if not ts: continue
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts[:26].rstrip("Z") + "+00:00")).total_seconds()
        if age > ALERT_WINDOW: continue
        for l in rec.get("body", "").splitlines():
            if done.search(l): busy = None
            elif start.search(l) and not busy: busy = f"{l.strip()[:100]} ({age:.0f} s ago)"
    return busy

if app == "car-hub" and "--force" not in sys.argv:
    busy = alert_in_flight()
    if busy: sys.exit(f"not deploying: an alert is still being sent ({busy}). A restart now would drop its "
                      "photo, clip or incident report. Wait for it to finish, or pass --force.")
if "--check" in sys.argv: print("no alert in flight"); sys.exit(0)

t0 = time.time()
stamp = lambda: f"[{time.time() - t0:5.0f}s]"

subprocess.run([sys.executable, "-I", "make_compose.py"], cwd=root, check=True)
# Start following before deploying so no early line is missed.
logs = subprocess.Popen([wendy, "device", "logs", app, "--device", DEVICE],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
r = subprocess.run([wendy, "run", "--detach", "--device", DEVICE], cwd=root, capture_output=True, text=True)
if r.returncode != 0:
    logs.kill(); print(r.stdout[-2000:], r.stderr[-2000:]); sys.exit("deploy failed")
print(stamp(), "deployed", app, flush=True)

status = 1
boards = None          # atech-flasher: the boards it flashed ("FLASHED: car=/dev/ttyACM0 passenger=/dev/ttyACM1")
deploy_start = datetime.fromtimestamp(t0, timezone.utc)
watchdog = threading.Timer(timeout, logs.kill); watchdog.start()   # stop even if the app goes quiet
try:
    for line in logs.stdout:
        try: rec = json.loads(line)
        except Exception: rec = {"body": line}
        if rec.get("service") == "wendy-agent": continue
        ts = rec.get("timestamp")
        if ts and datetime.fromisoformat(ts[:26].rstrip("Z") + "+00:00") < deploy_start: continue  # previous run
        body = rec.get("body", "")
        for l in body.splitlines():
            if any(k in l for k in show + FAIL):
                print(stamp(), l[:160], flush=True)
        if "FLASHED:" in body:
            boards = set(re.findall(r"(\w+)=/dev/", body.split("FLASHED:", 1)[1].splitlines()[0]))
        if app == "atech-flasher" and boards is not None:
            for l in body.splitlines():
                boards -= {b for b in boards if f'"key":"{b}"' in l and ready in l}
            if not boards: status = 0; print(stamp(), "UP"); break
        elif ready in body: status = 0; print(stamp(), "UP"); break
        if any(k in body for k in FAIL): print(stamp(), "FAILED"); break
    else:
        print(stamp(), "TIMEOUT: no ready line")
finally:
    watchdog.cancel(); logs.kill()
sys.exit(status)
