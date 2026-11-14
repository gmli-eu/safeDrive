# Deploy an app to the Jetson and follow its logs until it is up, then stop.
#   python3 tools/deploy.py atech-flasher     (waits for the board's "mqtt: connected")
#   python3 tools/deploy.py jetson-hub        (waits for the hub's "MQTT connected")
import json, os, pathlib, shutil, subprocess, sys, threading, time
from datetime import datetime, timezone

DEVICE = os.environ.get("WENDY_DEVICE", "wendyos-hackathon-device-4.local")
APPS = {  # folder -> (wendy app name, line that means "up", log lines worth showing)
    "atech-flasher": ("atech-flasher", "mqtt: connected",
                      ("tools ready", "toolchain changed", "built after", "SUCCESS", "+ atech", "FLASH_OK", "wifi:", "mqtt:")),
    "jetson-hub": ("car-hub", "MQTT connected", ("MQTT connected", "Telegram bot", "waiting for broker", "[vision]")),
}
FAIL = (" failed", "rror:", "Traceback", "No Atech board")

folder = sys.argv[1].strip("/").split("/")[-1]
if folder == "unoq-camera":   # not a Wendy app: copied to the Uno Q over SSH, runs as a systemd service
    root = pathlib.Path(__file__).resolve().parent.parent / folder
    secrets = json.loads((root / "secrets.json").read_text())
    host = os.environ.get("UNOQ_HOST", secrets["host"])
    key = pathlib.Path.home() / ".ssh/unoq"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *(["-i", str(key)] if key.exists() else []),
           f"{secrets.get('ssh_user', 'arduino')}@{host}"]
    unit = (root / "safedrive-camera.service").read_text()
    subprocess.run(ssh + ["mkdir -p ~/safedrive-camera && cat > ~/safedrive-camera/stream.py"],
                   input=(root / "stream.py").read_bytes(), check=True)
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
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 600
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
