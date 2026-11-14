# Runs on the Arduino UNO Q: serves the USB camera's own MJPEG frames over HTTP, without
# re-encoding (v4l2-ctl reads the camera, this splits the stream into JPEGs). Standard library only.
#   GET /stream.mjpg  live multipart MJPEG (for a browser or the Jetson)
#   GET /frame.jpg    newest frame, 503 if none in the last 2 s
#   GET /health       JSON: fps, age of the newest frame, camera, size, dark (frames near-black)
# Every frame carries X-Captured-At-Unix-Ns, as Wendy's camera sharing expects.
import glob, json, os, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

WIDTH, HEIGHT = int(os.environ.get("WIDTH", 1280)), int(os.environ.get("HEIGHT", 720))
FPS = int(os.environ.get("FPS", 15))
PORT = int(os.environ.get("PORT", 8090))
POWER_LINE = os.environ.get("POWER_LINE", "1")   # 1 = 50 Hz (Europe): no flicker under artificial light
MAX_AGE = 2.0

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def find_camera():
    # The first capture device that isn't the chip's video encoder/decoder (qcom-venus).
    for path in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
        name = open(path + "/name").read().strip()
        if "venus" in name: continue
        dev = "/dev/" + os.path.basename(path)
        fmts = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats"], capture_output=True, text=True).stdout
        if "MJPG" in fmts: return dev, name
    return None, None

# The camera's auto exposure can get stuck near zero: auto mode stays on, but every frame is black.
# Black frames are tiny JPEGs (~19 KB at 1280x720, a real picture is 90-130 KB), so when frames stay
# that small for DARK_KICK_AFTER s, nudge auto exposure: manual at a long exposure, then back to auto.
# A covered lens or a dark car gives small frames too; there the nudge is harmless and just repeats
# every DARK_KICK_EVERY s.
DARK_BYTES_PER_PIXEL = 0.035
DARK_KICK_AFTER, DARK_KICK_EVERY = 10, 60

def kick_exposure(dev):
    for ctrl in ("auto_exposure=1", "exposure_time_absolute=10000", "auto_exposure=0"):   # one at a time:
        subprocess.run(["v4l2-ctl", "-d", dev, "-c", ctrl], capture_output=True)            # order matters
        time.sleep(0.5)

latest = {"jpg": None, "ns": 0, "seq": 0}
cond = threading.Condition()
stats = {"device": None, "name": None, "fps": 0.0, "dark": False}

def capture_loop():
    while True:
        dev, name = find_camera()
        if not dev:
            stats.update(device=None, name=None, fps=0.0)
            log("no camera found, retrying in 3 s"); time.sleep(3); continue
        stats.update(device=dev, name=name)
        subprocess.run(["v4l2-ctl", "-d", dev, "-c", f"power_line_frequency={POWER_LINE}"], capture_output=True)
        cmd = ["v4l2-ctl", "-d", dev, f"--set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=MJPG",
               f"--set-parm={FPS}", "--stream-mmap", "--stream-count=0", "--stream-to=-"]
        log(f"streaming {name} ({dev}) {WIDTH}x{HEIGHT} MJPEG @ {FPS} fps on port {PORT}")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        buf, count, t0 = b"", 0, time.time()
        dark_limit = DARK_BYTES_PER_PIXEL * WIDTH * HEIGHT
        dark_since, last_kick = None, 0.0
        try:
            while True:
                chunk = p.stdout.read(65536)
                if not chunk: break
                buf += chunk
                while True:   # cut complete JPEGs (FFD8 ... FFD9) out of the byte stream
                    s = buf.find(b"\xff\xd8\xff")
                    if s < 0: buf = buf[-2:]; break
                    e = buf.find(b"\xff\xd9", s + 3)
                    if e < 0: buf = buf[s:]; break
                    jpg, buf = buf[s:e + 2], buf[e + 2:]
                    with cond:
                        latest.update(jpg=jpg, ns=time.time_ns(), seq=latest["seq"] + 1)
                        cond.notify_all()
                    count += 1
                    now = time.time()
                    stats["dark"] = len(jpg) < dark_limit
                    if len(jpg) >= dark_limit:
                        if dark_since and last_kick > dark_since: log("picture back after exposure nudge")
                        dark_since = None
                    elif dark_since is None:
                        dark_since = now
                    elif now - dark_since > DARK_KICK_AFTER and now - last_kick > DARK_KICK_EVERY:
                        last_kick = now
                        log(f"frames black for {now - dark_since:.0f}s ({len(jpg) // 1024} KB): nudging auto exposure")
                        threading.Thread(target=kick_exposure, args=(dev,), daemon=True).start()
                    if time.time() - t0 >= 5:
                        stats["fps"] = round(count / (time.time() - t0), 1); count, t0 = 0, time.time()
        finally:
            p.kill(); p.wait()
        stats["fps"] = 0.0
        log("camera stream ended (unplugged?), restarting in 2 s"); time.sleep(2)

PAGE = b"""<!doctype html><meta name=viewport content="width=device-width"><title>SafeDrive camera</title>
<body style="margin:0;background:#000"><img src="/stream.mjpg" style="width:100%;height:100vh;object-fit:contain">"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def frame_headers(self, jpg, ns):
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("X-Captured-At-Unix-Ns", str(ns))
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/frame.jpg":
            with cond: jpg, ns = latest["jpg"], latest["ns"]
            if not jpg or time.time_ns() - ns > MAX_AGE * 1e9:
                return self.send_error(503, "no recent frame")
            self.send_response(200); self.frame_headers(jpg, ns)
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(jpg)
        elif path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store"); self.end_headers()
            seq = 0
            try:
                while True:
                    with cond:
                        cond.wait_for(lambda: latest["seq"] != seq, timeout=5)
                        if latest["seq"] == seq: continue
                        jpg, ns, seq = latest["jpg"], latest["ns"], latest["seq"]
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\nX-Captured-At-Unix-Ns: %d\r\n\r\n" % (len(jpg), ns)
                                     + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/health":
            with cond: ns = latest["ns"]
            body = json.dumps({**stats, "width": WIDTH, "height": HEIGHT,
                               "last_frame_age_s": round((time.time_ns() - ns) / 1e9, 2) if ns else None}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path == "/":
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)
        else:
            self.send_error(404)

threading.Thread(target=capture_loop, daemon=True).start()
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
