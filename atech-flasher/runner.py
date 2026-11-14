# Runs on the Jetson: compile the Atech firmware and flash it over USB, then stream its serial output.
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

proj = pathlib.Path("/work/atech_car")
proj.mkdir(parents=True, exist_ok=True)
import base64, zlib   # compressed by make_compose.py: Wendy caps each env var at 32 KB
(proj / "project.yaml").write_bytes(zlib.decompress(base64.b64decode(os.environ["PROJECT_B64"])))

# PlatformIO's build output (compiled objects + downloaded libraries). Restoring it lets the
# build recompile only what changed instead of the whole Arduino framework every deploy.
CACHE_BUILD, RUN_BUILD = "/cache/build-pio", proj / "build" / ".pio"
if os.path.isdir(CACHE_BUILD) and not RUN_BUILD.exists():
    RUN_BUILD.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-a", CACHE_BUILD, str(RUN_BUILD)], check=True)

ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
print("serial ports:", ports, flush=True)
port = os.environ.get("ATECH_PORT") or (ports[0] if ports else "")

if run("atech", "validate", str(proj)) != 0:
    sys.exit("validate failed")
if run("atech", "build", str(proj)) != 0:
    sys.exit("build failed")
if not os.path.isdir(CACHE_PIO) or toolchain_fingerprint() != toolchain_before:
    print("toolchain changed: updating the cache...", flush=True)
    run("sh", "-c", f"rm -rf {CACHE_PIO}.new && cp -a {RUN_PIO} {CACHE_PIO}.new && rm -rf {CACHE_PIO} && mv {CACHE_PIO}.new {CACHE_PIO}")
print(f"built after {time.time() - t0:.0f}s", flush=True)
run("sh", "-c", f"rm -rf {CACHE_BUILD}.new && cp -a {RUN_BUILD} {CACHE_BUILD}.new && rm -rf {CACHE_BUILD} && mv {CACHE_BUILD}.new {CACHE_BUILD}")
if not port:
    print("No Atech board on USB: firmware built but not flashed.", flush=True)
    sys.exit(1)
if run("atech", "upload", str(proj), "--port", port) != 0:
    sys.exit("upload failed")
time.sleep(2)
run("atech", "check", "--port", port)
print("FLASH_OK: streaming board output (Ctrl-C / stop the app to end)", flush=True)
os.execvp("atech", ["atech", "monitor", "--json", "--port", port])
