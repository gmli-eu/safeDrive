# Runs on the Jetson: compile the Atech firmware for both boards (driver and back-seat intercom),
# flash each over USB, then stream their serial output.
import glob, os, pathlib, subprocess, sys, time

# The persistent volume is mounted noexec, so nothing can run from it directly. Keep the
# ~2.4 GB toolchain and the installed atech tool cached there, and copy them into the
# container filesystem to run them.
ATECH_VERSION = "1.0.0a12"
CACHE_PIO, RUN_PIO = "/cache/pio", "/root/.platformio"
CACHE_SITE, RUN_SITE = f"/cache/atech-site-{ATECH_VERSION}", "/opt/atech-site"
os.environ["PLATFORMIO_CORE_DIR"] = RUN_PIO
os.environ["PYTHONPATH"] = RUN_SITE
os.environ["PATH"] = f"{RUN_SITE}/bin:" + os.environ["PATH"]

def run(*args):
    print("+", " ".join(args), flush=True)
    return subprocess.run(args).returncode

def toolchain_fingerprint():
    # Installed PlatformIO platforms/packages; the cache is rewritten only when this changes.
    return sorted(str(p.relative_to(RUN_PIO)) for d in ("packages", "platforms")
                  for p in pathlib.Path(RUN_PIO, d).glob("*")) if os.path.isdir(RUN_PIO) else None

t0 = time.time()
if os.path.isdir(CACHE_PIO) and not os.path.isdir(RUN_PIO):
    print("copying cached toolchain into the container...", flush=True)
    subprocess.run(["cp", "-a", CACHE_PIO, RUN_PIO], check=True)
toolchain_before = toolchain_fingerprint()

# atech + platformio + deps: installed once (over the network), then reused from the cache.
if os.path.isdir(CACHE_SITE):
    subprocess.run(["cp", "-a", CACHE_SITE, RUN_SITE], check=True)
else:
    if run(sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore", "--disable-pip-version-check",
           "--cache-dir", "/cache/pip", "--target", RUN_SITE, f"atech=={ATECH_VERSION}") != 0:
        sys.exit("pip install failed")
    run("sh", "-c", f"rm -rf {CACHE_SITE}.new && cp -a {RUN_SITE} {CACHE_SITE}.new && mv {CACHE_SITE}.new {CACHE_SITE}")
print(f"tools ready after {time.time() - t0:.0f}s", flush=True)

# Two boards: the driver's (project.yaml, "car") and the back-seat intercom (passenger.yaml).
import base64, zlib   # compressed by make_compose.py: Wendy caps each env var at 32 KB
FIRMWARE = {"car": "PROJECT_B64", "passenger": "PASSENGER_B64"}
projects = {}
for role, var in FIRMWARE.items():
    proj = pathlib.Path(f"/work/atech_{role}")
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "project.yaml").write_bytes(zlib.decompress(base64.b64decode(os.environ[var])))
    projects[role] = proj
    if run("atech", "validate", str(proj)) != 0:
        sys.exit(f"validate failed ({role})")

# ---------- which USB port is which board ----------
# Each firmware logs with its own key ("car" / "passenger"); the driver board also streams its
# orientation sensor, the passenger board its mic level as rearmic_level. Listen to each port for
# a few seconds, and remember each board's USB serial number in the cache, so a board that is crash-looping or blank is still known next time.
# A port that can't be told apart is the board that's left over; one unknown board alone is the driver's.
sys.path.insert(0, RUN_SITE)
import json, serial
from serial.tools import list_ports
BOARDS_FILE = pathlib.Path("/cache/boards.json")
try: known = json.loads(BOARDS_FILE.read_text())
except Exception: known = {}
usb_serial = {p.device: p.serial_number or "" for p in list_ports.comports()}

def sniff(port, seconds=4.0):
    # Opened like `atech monitor` does (pyserial's default DTR/RTS): with DTR off, the ESP32-S3's
    # USB serial stays silent. Reads in chunks with a hard deadline: a line may never end.
    try: s = serial.Serial(port=port, baudrate=115200, timeout=0.2, exclusive=True)
    except Exception as e:
        print(f"{port}: can't open: {e}", flush=True); return None
    end, seen, role = time.time() + seconds, "", None
    with s:
        while time.time() < end and not role:
            seen = (seen + s.read(4096).decode("utf-8", "replace"))[-8000:]
            if '"key":"passenger"' in seen or "rearmic_level" in seen: role = "passenger"
            elif '"key":"car"' in seen or "icm40608" in seen or "imu_" in seen: role = "car"
    if not role: print(f"{port}: heard {seen[-200:]!r}", flush=True)
    return role

ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
print("serial ports:", ports, flush=True)
roles = {}
for port in ports:
    role = sniff(port)
    how = "heard it"
    if not role and known.get(usb_serial.get(port)): role, how = known[usb_serial[port]], "remembered"
    print(f"{port} (usb serial {usb_serial.get(port) or '?'}): {role or 'unknown'}" + (f" ({how})" if role else ""), flush=True)
    roles[port] = role
if len(set(r for r in roles.values() if r)) < len([r for r in roles.values() if r]):
    sys.exit(f"two boards look like the same one: {roles}. Unplug one, deploy, then plug it back in.")
unknown = [p for p, r in roles.items() if not r]
missing = [r for r in FIRMWARE if r not in roles.values()]
if len(unknown) == 1 and missing:
    roles[unknown[0]] = "car" if "car" in missing else missing[0]
    print(f"{unknown[0]}: taken to be the {roles[unknown[0]]} board (the one left over)", flush=True)
elif unknown:
    sys.exit(f"can't tell the boards apart: {roles}. Unplug the passenger board, deploy, plug it back in and deploy again.")
for port, role in roles.items():
    if usb_serial.get(port): known[usb_serial[port]] = role
try: BOARDS_FILE.write_text(json.dumps(known))
except Exception as e: print("can't save the board list:", e, flush=True)
if os.environ.get("ATECH_PORT"): roles = {os.environ["ATECH_PORT"]: "car"}

# ---------- build ----------
for role in sorted(set(roles.values()) or {"car"}):
    proj = projects[role]
    # PlatformIO's build output (compiled objects + downloaded libraries). Restoring it lets the
    # build recompile only what changed instead of the whole Arduino framework every deploy.
    cache_build = "/cache/build-pio" + ("" if role == "car" else f"-{role}")
    run_build = proj / "build" / ".pio"
    if os.path.isdir(cache_build) and not run_build.exists():
        run_build.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-a", cache_build, str(run_build)], check=True)
    if run("atech", "build", str(proj)) != 0:
        sys.exit(f"build failed ({role})")
    print(f"built {role} after {time.time() - t0:.0f}s", flush=True)
    run("sh", "-c", f"rm -rf {cache_build}.new && cp -a {run_build} {cache_build}.new && rm -rf {cache_build} && mv {cache_build}.new {cache_build}")
if not os.path.isdir(CACHE_PIO) or toolchain_fingerprint() != toolchain_before:
    print("toolchain changed: updating the cache...", flush=True)
    run("sh", "-c", f"rm -rf {CACHE_PIO}.new && cp -a {RUN_PIO} {CACHE_PIO}.new && rm -rf {CACHE_PIO} && mv {CACHE_PIO}.new {CACHE_PIO}")
if not roles:
    print("No Atech board on USB: firmware built but not flashed.", flush=True)
    sys.exit(1)

# ---------- flash, then stream every board's output ----------
for port, role in roles.items():
    if run("atech", "upload", str(projects[role]), "--port", port) != 0:
        sys.exit(f"upload failed ({role} board on {port})")
time.sleep(2)
print("FLASHED: " + " ".join(f"{r}={p}" for p, r in roles.items()), flush=True)
# `atech check` reboots the board; start following it right after, so its Wi-Fi and MQTT lines
# (a few seconds after boot) aren't missed while the next board is checked.
monitors = []
for port in roles:
    run("atech", "check", "--port", port)
    monitors.append(subprocess.Popen(["atech", "monitor", "--json", "--port", port]))
print("FLASH_OK: streaming board output (Ctrl-C / stop the app to end)", flush=True)
while all(m.poll() is None for m in monitors): time.sleep(1)
for m in monitors:
    if m.poll() is None: m.terminate()
sys.exit("a board's monitor stopped")
