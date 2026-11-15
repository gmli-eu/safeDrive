# SafeDrive camera service (runs on the Jetson, next to hub.py).
#   Uno Q camera --MJPEG over HTTP--> keeps the last BUFFER_S seconds of frames in RAM
#   hub.py asks for the newest photo or a clip of a time window when an SOS or accident alert goes out:
#     GET http://127.0.0.1:8091/photo.jpg            newest frame (503 if the camera is silent)
#     GET http://127.0.0.1:8091/clip.mp4?start=&end=  H.264 MP4 of [start, end] (Unix seconds); waits for end
#     GET http://127.0.0.1:8091/frames?n=4&span=1.5  n frames over the last span s, JSON with base64 JPEGs
#     GET http://127.0.0.1:8091/health
# Frames only ever live in memory; nothing is written to disk except the clip being encoded.
import os, subprocess, sys, shutil

# ---------- dependencies: installed once into the data volume, copied in on every start ----------
# The volume is noexec (same as voice.py): keep the packages there, run them from a copy.
PKGS = ["imageio-ffmpeg==0.8.1"]
CACHE_SITE = "/data/site-vision-1"          # bump the number when PKGS changes
RUN_SITE = "/opt/vision-site"
if not os.path.isdir(RUN_SITE):
    if os.path.isdir(CACHE_SITE):
        subprocess.run(["cp", "-a", CACHE_SITE, RUN_SITE], check=True)
    else:
        print("installing vision packages (first start only)...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore",
                        "--disable-pip-version-check", "--cache-dir", "/data/pip", "--target", RUN_SITE, *PKGS], check=True)
        shutil.rmtree(CACHE_SITE + ".new", True)
        subprocess.run(["cp", "-a", RUN_SITE, CACHE_SITE + ".new"], check=True)
        os.rename(CACHE_SITE + ".new", CACHE_SITE)
sys.path.insert(0, RUN_SITE)

import base64, bisect, collections, json, tempfile, threading, time, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import imageio_ffmpeg

CAMERA_URL = os.environ.get("CAMERA_URL", "http://172.20.10.4:8090/stream.mjpg")
PORT = int(os.environ.get("VISION_PORT", "8091"))
BUFFER_S = float(os.environ.get("BUFFER_S", "40"))       # seconds of frames kept in RAM
PHOTO_MAX_AGE = 3.0                                      # a "photo" older than this means the camera is gone
CLIP_MAX_WAIT = 30.0                                     # longest a clip request waits for its end time
CLIP_WIDTH = int(os.environ.get("CLIP_WIDTH", "960"))    # clips are scaled down: smaller upload over the hotspot

STALL_S = 0.4                # a gap between frames longer than this is logged (hotspot stall)

def log(*a): print(time.strftime("%H:%M:%S"), "[vision]", *a, flush=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
_enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "27"] if " libx264 " in _enc else ["-c:v", "mpeg4", "-q:v", "6"]
log("clip encoder:", CODEC[1], "(Telegram plays H.264 inline; mpeg4 may only download)" if CODEC[1] != "libx264" else "")

# ---------- frame buffer ----------
frames = collections.deque()      # (unix seconds, jpeg bytes), oldest first
lock = threading.Lock()
cam = {"connected": False, "fps": 0.0, "since": None}

def add_frame(ts, jpg):
    with lock:
        frames.append((ts, jpg))
        while frames and frames[0][0] < ts - BUFFER_S: frames.popleft()

def read_stream():
    # multipart/x-mixed-replace from unoq-camera/stream.py: each part has Content-Length and
    # X-Captured-At-Unix-Ns. Reconnects forever; logs only when the state changes.
    delay = 1
    while True:
        try:
            r = urllib.request.urlopen(CAMERA_URL, timeout=5)
            n, t0 = 0, time.time()
            while True:
                line = r.readline()
                if not line: raise ConnectionError("stream ended")
                if line.strip() != b"--frame": continue
                h = {}
                while (line := r.readline().strip()):
                    k, _, v = line.partition(b":"); h[k.strip().lower()] = v.strip()
                jpg = r.read(int(h[b"content-length"]))
                # Our own clock, not the Uno Q's: alert times come from this machine too.
                now = time.time()
                # At 10 fps a frame is due every 0.1 s: a long gap means the hotspot stalled
                # (the speaker's audio goes through the same hotspot, so it shows why speech broke up).
                with lock: last = frames[-1][0] if frames else None
                if last and cam["connected"] and now - last > STALL_S: log(f"camera stall: no frame for {now - last:.2f} s")
                add_frame(now, jpg)
                if not cam["connected"]:
                    cam.update(connected=True, since=time.time()); delay = 1
                    log("camera connected:", CAMERA_URL)
                n += 1
                if time.time() - t0 >= 10:
                    cam["fps"] = round(n / (time.time() - t0), 1); n, t0 = 0, time.time()
        except Exception as e:
            if cam["connected"] or delay == 1:
                log(f"camera unavailable ({e.__class__.__name__}: {str(e)[:80]}), retrying")
            cam.update(connected=False, fps=0.0)
            time.sleep(delay); delay = min(delay * 2, 10)

def window(start, end):
    with lock: snap = list(frames)
    i = bisect.bisect_left([t for t, _ in snap], start)
    return [(t, j) for t, j in snap[i:] if t <= end]

def encode_clip(clip):
    # Frames arrive at an uneven rate, so feed them through the concat demuxer with their real
    # durations: the clip plays at true speed.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        lines = []
        for k, (t, jpg) in enumerate(clip):
            p = f"{d}/{k:05d}.jpg"
            with open(p, "wb") as f: f.write(jpg)
            nxt = clip[k + 1][0] if k + 1 < len(clip) else t + 1 / 15
            lines += [f"file '{p}'", f"duration {max(nxt - t, 0.001):.4f}"]
        lines.append(f"file '{d}/{len(clip) - 1:05d}.jpg'")   # concat demuxer quirk: repeat the last file
        with open(f"{d}/list.txt", "w") as f: f.write("\n".join(lines) + "\n")
        out = f"{d}/clip.mp4"
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", f"{d}/list.txt", "-vf", f"scale={CLIP_WIDTH}:-2,fps=15", *CODEC,
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", out],
                           capture_output=True, text=True)
        if r.returncode: raise RuntimeError("ffmpeg: " + r.stderr[-300:])
        with open(out, "rb") as f: return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        url = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/photo.jpg":
                with lock: last = frames[-1] if frames else None
                if not last or time.time() - last[0] > PHOTO_MAX_AGE:
                    return self.reply(503, b"no recent frame", "text/plain")
                self.send_response(200); self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Captured-At", f"{last[0]:.3f}")
                self.send_header("Content-Length", str(len(last[1]))); self.end_headers(); self.wfile.write(last[1])
            elif url.path == "/frames":
                # The newest frame plus n-1 earlier ones, evenly spread over the last `span` seconds
                # (oldest first), so a model sees motion. JSON: {"frames": [{"captured_at", "jpeg_b64"}]}.
                n = max(1, min(int(q.get("n", ["4"])[0]), 8))
                span = max(0.0, min(float(q.get("span", ["1.5"])[0]), 10.0))
                with lock: snap = list(frames)
                if not snap or time.time() - snap[-1][0] > PHOTO_MAX_AGE:
                    return self.reply(503, b"no recent frame", "text/plain")
                newest = snap[-1][0]; times = [t for t, _ in snap]; picked = []
                for k in range(n):
                    target = newest - span * (n - 1 - k) / max(n - 1, 1)
                    i = min(bisect.bisect_left(times, target), len(snap) - 1)
                    if i > 0 and target - times[i - 1] < times[i] - target: i -= 1
                    if not picked or picked[-1] != i: picked.append(i)   # never the same frame twice
                body = json.dumps({"frames": [{"captured_at": round(snap[i][0], 3),
                                               "jpeg_b64": base64.b64encode(snap[i][1]).decode()} for i in picked]}).encode()
                self.reply(200, body, "application/json")
            elif url.path == "/clip.mp4":
                end = float(q.get("end", [time.time()])[0]); start = float(q.get("start", [end - 10])[0])
                wait = min(end - time.time(), CLIP_MAX_WAIT)
                if wait > 0: time.sleep(wait + 0.3)
                clip = window(start, end)
                if len(clip) < 5: return self.reply(404, b"no frames in that window", "text/plain")
                t = time.time(); mp4 = encode_clip(clip)
                log(f"clip {clip[-1][0] - clip[0][0]:.1f}s, {len(clip)} frames, {len(mp4) // 1024} KB, encoded in {time.time() - t:.1f}s")
                self.reply(200, mp4, "video/mp4")
            elif url.path == "/health":
                with lock: n, oldest = len(frames), frames[0][0] if frames else None
                self.reply(200, json.dumps({**cam, "buffered_frames": n, "codec": CODEC[1],
                    "buffered_s": round(time.time() - oldest, 1) if oldest else 0}).encode(), "application/json")
            else:
                self.reply(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("request failed:", self.path, e)
            try: self.reply(500, str(e).encode(), "text/plain")
            except Exception: pass

threading.Thread(target=read_stream, daemon=True).start()
ThreadingHTTPServer.daemon_threads = True
log(f"serving on 127.0.0.1:{PORT}, buffer {BUFFER_S:.0f}s")
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
