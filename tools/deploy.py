# Deploy an app to the Jetson and follow its logs until it is up, then stop.
#   python3 tools/deploy.py atech-flasher     (waits for the board's "mqtt: connected")
#   python3 tools/deploy.py jetson-hub        (waits for the hub's "MQTT connected")
#   python3 tools/deploy.py unoq-camera       (copies the camera streamer to the Uno Q over SSH)
# jetson-hub refuses to deploy while an SOS/accident/threat alert is still being sent: a restart
# kills the photo/clip upload. --check only runs that check; --force deploys anyway.
import re
import json, os, pathlib, shutil, subprocess, sys, threading, time
from datetime import datetime, timezone

DEVICE = os.environ.get("WENDY_DEVICE", "wendyos-hackathon-device-4.local")
APPS = {  # folder -> (wendy app name, line that means "up", log lines worth showing)
    "atech-flasher": ("atech-flasher", "mqtt: connected",
                      ("tools ready", "toolchain changed", "built after", "SUCCESS", "+ atech", "FLASH_OK", "wifi:", "mqtt:")),
    "jetson-hub": ("car-hub", "MQTT connected", ("MQTT connected", "Telegram bot", "waiting for broker", "[vision]", "[dash]")),
}
FAIL = (" failed", "rror:", "Traceback", "No Atech board")

args = [a for a in sys.argv[1:] if not a.startswith("--")]
folder = args[0].strip("/").split("/")[-1]
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
    subprocess.run(ssh + ["rm -rf ~/safedrive-camera/icons && mkdir -p ~/safedrive-camera && tar -xzf - -C ~/safedrive-camera"],
                   input=buf.getvalue(), check=True)
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
            print("UP", health, f"\nview: http://{host}:8090/"); sys.exit(0)
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
        if ready in body: status = 0; print(stamp(), "UP"); break
        if any(k in body for k in FAIL): print(stamp(), "FAILED"); break
    else:
        print(stamp(), "TIMEOUT: no ready line")
finally:
    watchdog.cancel(); logs.kill()
sys.exit(status)
