# Runs on the Arduino UNO Q: serves the USB camera's own MJPEG frames over HTTP, without
# re-encoding (v4l2-ctl reads the camera, this splits the stream into JPEGs). Standard library only.
#   GET /              live view: the preview, on-device person boxes, the Thermo reading, hand-sign guide,
#                      and "What NVIDIA Nemotron sees" (the browser reads it from the Jetson: dash.py /nemotron)
#   GET /stream.mjpg   live multipart MJPEG at full FPS (for the Jetson)
#   GET /preview.mjpg  live multipart MJPEG capped at ~2.5 fps (for browsers: spares 2.4 GHz Wi-Fi)
#   GET /frame.jpg     newest frame, 503 if none in the last 2 s
#   GET /health        JSON: fps, age of the newest frame, camera, size, dark (frames near-black)
#   GET /icons/*.png   hand-sign guide icons shown in the live view's menu
#   GET /climate       JSON: cabin temperature and humidity from the Modulino Thermo (see thermo/)
#   GET /occupancy     JSON: people in the cabin, counted on this board by an Edge Impulse model.
#                      The hub adds ?cloud=1 (or 0) when frames are (or aren't) going to Nemotron.
# Every frame carries X-Captured-At-Unix-Ns, as Wendy's camera sharing expects.
import glob, json, os, pathlib, socket, struct, subprocess, threading, time, urllib.request
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

# ---------- cabin climate: Modulino Thermo on the microcontroller ----------
# The sketch in thermo/ runs on the board's STM32 and offers "thermo_read" through the Arduino
# router (MessagePack-RPC on a Unix socket). Only the few MessagePack types used here are handled.
ROUTER_SOCK = os.environ.get("ROUTER_SOCK", "/var/run/arduino-router.sock")
climate = {"celsius": None, "humidity": None, "at": 0.0, "error": "no reading yet"}

def mp_pack(o):
    if isinstance(o, list): return bytes([0x90 | len(o)]) + b"".join(mp_pack(x) for x in o)
    if isinstance(o, bool): return b"\xc3" if o else b"\xc2"          # before int: a bool is an int
    if isinstance(o, int):
        if 0 <= o < 128: return bytes([o])
        if -32 <= o < 0: return struct.pack(">b", o)                    # negative fixint
        return b"\xd2" + struct.pack(">i", o)
    b = o.encode(); return bytes([0xa0 | len(b)]) + b if len(b) < 32 else b"\xd9" + bytes([len(b)]) + b

def mp_unpack(b, i=0):
    t = b[i]
    if t <= 0x7f: return t, i + 1
    if t >= 0xe0: return t - 256, i + 1
    if 0x90 <= t <= 0x9f or t == 0xdc:
        n, i = (t & 15, i + 1) if t != 0xdc else (struct.unpack(">H", b[i+1:i+3])[0], i + 3)
        out = []
        for _ in range(n): v, i = mp_unpack(b, i); out.append(v)
        return out, i
    if 0xa0 <= t <= 0xbf: n = t & 31; return b[i+1:i+1+n].decode(), i + 1 + n
    if t == 0xd9: n = b[i+1]; return b[i+2:i+2+n].decode(), i + 2 + n
    if t == 0xc0: return None, i + 1
    if t in (0xc2, 0xc3): return t == 0xc3, i + 1
    fixed = {0xcc: ">B", 0xcd: ">H", 0xce: ">I", 0xd0: ">b", 0xd1: ">h", 0xd2: ">i", 0xca: ">f", 0xcb: ">d"}
    if t in fixed:
        n = struct.calcsize(fixed[t]); return struct.unpack(fixed[t], b[i+1:i+1+n])[0], i + 1 + n
    raise ValueError(f"MessagePack type {t:#x} not handled")

def thermo_read(msgid): return router_call(msgid, "thermo_read", [])

def router_call(msgid, method, params):
    # One MessagePack-RPC request to a function the sketch offers, through the Arduino router.
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3); s.connect(ROUTER_SOCK)
        s.sendall(mp_pack([0, msgid, method, params]))
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk: raise ConnectionError("router closed the connection")
            buf += chunk
            try: (kind, rid, err, res), _ = mp_unpack(buf); break
            except (IndexError, struct.error): continue   # message not complete yet
    if err: raise RuntimeError(f"router: {err}")   # e.g. the sketch isn't running
    return res

def climate_loop():
    msgid, last_error = 0, None
    while True:
        msgid = msgid % 1000 + 1
        try:
            res = thermo_read(msgid)
            if not res: raise RuntimeError("Modulino Thermo not found on the Qwiic connector")
            c, h, age_ms = (float(x) for x in res.split(","))
            climate.update(celsius=round(c, 1), humidity=round(h), at=time.time() - age_ms / 1000, error=None)
            if last_error: log(f"thermo back: {c:.1f} C, {h:.0f} %")
            last_error = None
        except Exception as e:
            climate["error"] = str(e)[:120]
            if str(e) != last_error: log("thermo:", e)
            last_error = str(e)
        time.sleep(2)

# ---------- on-device person detection: Edge Impulse YOLOX-nano on this board ----------
# Arduino's Edge Impulse model runner (the ei-models-runner container from Arduino App Lab, started by
# tools/deploy.py as "safedrive-people") serves YOLOX-nano on 127.0.0.1:1340. About once a second the
# newest camera frame goes to it (~0.5 s per frame, offline). Nothing leaves the board.
# "empty" is only claimed when frames keep coming back without anyone: the Jetson pauses the cloud
# cabin watch only after EMPTY_FOR_S of that (fail-open: a missed detection costs little).
DETECT_URL = os.environ.get("DETECT_URL", "http://127.0.0.1:1340/api/image")
PERSON_MIN = float(os.environ.get("PERSON_MIN", "0.5"))   # a box this sure counts as a person
occupancy = {"people": None, "confidence": 0.0, "boxes": [], "at": 0.0, "empty_since": None,
             "ms": None, "error": "no detection yet"}

def detect(jpg):
    boundary = "sd" + os.urandom(8).hex()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"f.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n").encode() + jpg + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(DETECT_URL, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=10) as r: return json.load(r)["result"]

def occupancy_loop():
    last_error, last_people = None, -1
    while True:
        t0 = time.time()
        try:
            with cond: jpg, ns = latest["jpg"], latest["ns"]
            if not jpg or time.time_ns() - ns > MAX_AGE * 1e9: raise RuntimeError("no recent frame")
            if stats["dark"]: raise RuntimeError("picture too dark to tell")   # unknown, never "empty"
            res = detect(jpg)
            w, h = res["resized"]["originalWidth"], res["resized"]["originalHeight"]
            boxes = [{"label": b["label"], "conf": round(b["value"], 2),   # 0-1, relative to the frame
                      "x": round(b["x"] / w, 3), "y": round(b["y"] / h, 3), "w": round(b["width"] / w, 3), "h": round(b["height"] / h, 3)}
                     for b in res["bounding_boxes"] if b["label"] == "person"]
            people = sum(b["conf"] >= PERSON_MIN for b in boxes)
            now = time.time()
            occupancy.update(people=people, confidence=max([b["conf"] for b in boxes], default=0.0), boxes=boxes, at=now,
                             ms=round((now - t0) * 1000), error=None,
                             empty_since=None if boxes else (occupancy["empty_since"] or now))   # any person box, even unsure, isn't empty
            if last_error: log("person detection back")
            if people != last_people: log(f"on-device: {people} {'person' if people == 1 else 'people'} ({(now - t0):.2f}s)")
            last_error, last_people = None, people
        except Exception as e:
            occupancy.update(people=None, boxes=[], empty_since=None, error=str(e)[:120])
            if str(e) != last_error: log("person detection:", e)
            last_error, last_people = str(e), -1
        time.sleep(max(0.2, 1.0 - (time.time() - t0)))

# ---------- the board's LED matrix: people count + cloud icon (drawn by the sketch) ----------
# The hub says with each /occupancy poll whether frames are going to Nemotron (?cloud=1). The sketch
# shows the on-device count (or "?" when it can't tell) and a cloud icon while frames go to the cloud.
cloud = {"active": None, "at": 0.0}

def cloud_now():
    return cloud["active"] if time.time() - cloud["at"] < 10 else None   # None: the hub hasn't said lately

def matrix_loop():
    shown, last_sent, msgid, last_error = None, 0.0, 0, None
    while True:
        o = occupancy_json()
        want = (-1 if o["people"] is None else min(o["people"], 9), bool(cloud_now()))
        if want != shown or time.time() - last_sent > 10:   # resent now and then: the sketch may have restarted
            msgid = msgid % 1000 + 1
            try:
                router_call(msgid, "matrix_show", list(want))
                if want != shown: log(f"matrix: {'?' if want[0] < 0 else want[0]}{' + cloud' if want[1] else ''}")
                shown, last_sent, last_error = want, time.time(), None
            except Exception as e:
                if str(e) != last_error: log("matrix:", e)
                last_error = str(e)
        time.sleep(0.5)

def occupancy_json():
    o, now = occupancy, time.time()
    fresh = o["people"] is not None and now - o["at"] < 10
    return {"people": o["people"] if fresh else None, "confidence": o["confidence"] if fresh else None,
            "empty_for_s": round(now - o["empty_since"], 1) if fresh and o["empty_since"] else 0.0,
            "boxes": o["boxes"] if fresh else [], "ms": o["ms"], "model": "Edge Impulse YOLOX-nano (on-device)",
            "cloud": cloud_now(),
            "error": None if fresh else (o["error"] or "detection too old")}

ICON_DIR = pathlib.Path(__file__).resolve().parent / "icons"
ICONS = {p.name: p.read_bytes() for p in ICON_DIR.glob("*.png")} if ICON_DIR.is_dir() else {}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>SafeDrive camera</title>
<style>
  :root { --bg:#0b0f14; --card:#151b23; --border:#232d3b; --fg:#e8edf2; --muted:#8a96a3; --accent:#2f80ed; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body { margin:0; height:100%; background:#000; color:var(--fg);
    font:15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; overflow:hidden; }
  body { display:flex; height:100dvh; }
  .stage { flex:1; min-width:0; position:relative; display:flex; align-items:center; justify-content:center; background:#000; }
  .feed { width:100%; height:100%; object-fit:contain; display:block; }
  .boxes { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
  .badges { position:absolute; left:14px; bottom:max(14px, env(safe-area-inset-bottom)); z-index:10;
    display:flex; flex-direction:column; align-items:flex-start; gap:6px; }
  .edge { padding:8px 13px;
    border-radius:999px; background:rgba(11,15,20,.82); border:1px solid rgba(255,255,255,.16); font-size:13px;
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); }
  .edge b { color:#4ade80; }
  .edge b.cloud { color:#76B900; }   /* NVIDIA green: frames are going to Nemotron */
  .edge b.local { color:#00979D; }   /* Arduino teal-blue: everything stays on the UNO Q */
  .edge.hot b { color:#f87171; }
  .menu-btn { position:absolute; top:max(14px, env(safe-area-inset-top)); right:14px; z-index:10;
    display:inline-flex; align-items:center; gap:8px; padding:10px 15px; border-radius:999px;
    border:1px solid rgba(255,255,255,.16); background:rgba(11,15,20,.82); color:var(--fg);
    font:600 14px/1 inherit; cursor:pointer; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    box-shadow:0 4px 18px rgba(0,0,0,.45); transition:background .15s, border-color .15s; }
  .menu-btn:hover { background:rgba(21,27,35,.95); border-color:rgba(255,255,255,.3); }
  .menu-btn[aria-expanded="true"] { background:var(--accent); border-color:var(--accent); color:#fff; }
  .menu-btn svg { width:18px; height:18px; flex:none; }
  .backdrop { display:none; }
  .drawer { width:410px; max-width:92vw; height:100%; background:var(--bg); border-left:1px solid var(--border);
    display:flex; flex-direction:column; overflow:hidden; }
  .drawer[hidden] { display:none !important; }
  .drawer-head { display:flex; align-items:center; justify-content:space-between; gap:12px;
    padding:16px 18px 12px; border-bottom:1px solid var(--border); }
  .drawer-head h2 { margin:0; font-size:17px; letter-spacing:.01em; }
  .close-btn { background:var(--card); border:1px solid var(--border); color:var(--fg); width:32px; height:32px;
    border-radius:50%; font-size:18px; line-height:1; cursor:pointer; display:grid; place-items:center; }
  .close-btn:hover { border-color:var(--muted); }
  .drawer-body { flex:1; overflow-y:auto; padding:14px 16px 22px; display:flex; flex-direction:column; gap:10px; }
  .tip { margin:0 0 2px; padding:10px 12px; border-radius:12px; background:rgba(47,128,237,.12);
    border:1px solid rgba(47,128,237,.28); color:var(--fg); font-size:13px; line-height:1.45; }
  .sign { display:flex; gap:13px; align-items:center; background:var(--card); border:1px solid var(--border);
    border-radius:16px; padding:11px 13px; cursor:pointer; transition:border-color .15s; text-align:left; color:inherit; font:inherit; width:100%; }
  .sign:hover, .sign.zoom { border-color:var(--accent); }
  .sign img { width:78px; height:78px; border-radius:12px; flex:none; object-fit:cover; background:#151b23;
    border:1px solid rgba(255,255,255,.06); transition:width .18s, height .18s; }
  .sign.zoom { flex-direction:column; align-items:flex-start; }
  .sign.zoom img { width:168px; height:168px; align-self:center; }
  .sign-info { min-width:0; flex:1; }
  .sign-name { font-weight:700; font-size:15px; margin-bottom:3px; display:flex; align-items:center; gap:6px; }
  .sign-how { color:var(--fg); font-size:13.5px; opacity:.92; }
  .sign-says { color:var(--muted); font-size:12.5px; margin-top:4px; font-style:italic; }
  .note { color:var(--muted); font-size:12.5px; line-height:1.45; margin:4px 2px 0; }
  /* What Nemotron sees: the cabin watch's latest verdicts, from the Jetson (dash.py GET /nemotron). */
  .nemo { position:absolute; top:max(14px, env(safe-area-inset-top)); left:14px; z-index:10; width:380px;
    max-width:calc(100% - 190px); padding:12px 14px; border-radius:16px; background:rgba(11,15,20,.84);
    border:1px solid rgba(118,185,0,.45); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    font-size:13px; line-height:1.4; }
  .nemo-head { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:12px; }
  .nemo-head b { color:#76B900; letter-spacing:.02em; }
  .nemo-what { font-size:15px; margin:7px 0 4px; }
  .nemo-row { color:var(--muted); margin-top:3px; }
  .nemo-row b { color:var(--fg); font-weight:600; }
  .lvl { display:inline-block; font:700 11px/1 inherit; letter-spacing:.06em; padding:4px 7px; border-radius:6px;
    margin-right:6px; vertical-align:2px; background:#1f3a12; color:#a3e635; }
  .lvl.watch { background:#4a3410; color:#fbbf24; }
  .lvl.danger { background:#5c1a1a; color:#fca5a5; }
  .said { color:#76B900; font-weight:600; }
  .nemo.stale { border-color:rgba(255,255,255,.16); }
  .nemo.stale .nemo-what { color:var(--muted); }
  .hist { margin:9px 0 0; padding:7px 0 0; border-top:1px solid rgba(255,255,255,.1); list-style:none;
    font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .hist li { display:flex; gap:8px; white-space:nowrap; overflow:hidden; }
  .hist li span:last-child { overflow:hidden; text-overflow:ellipsis; }
  .hist .d { color:#fca5a5; } .hist .w { color:#fbbf24; } .hist .s { color:#76B900; }
  @media (max-width: 780px) {
    .nemo { top:calc(max(14px, env(safe-area-inset-top)) + 50px); right:14px; width:auto; max-width:none; }
    .hist { display:none; }
  }
  @media (max-width: 780px) {
    .drawer { position:fixed; inset:0 0 0 auto; z-index:30; box-shadow:-12px 0 34px rgba(0,0,0,.7); }
    .backdrop.open { display:block; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:20; }
  }
</style></head>
<body>
  <div class="stage">
    <img class="feed" id="feed" src="/preview.mjpg" alt="SafeDrive live camera">
    <canvas class="boxes" id="boxes"></canvas>
    <section class="nemo stale" id="nemo" aria-live="polite" hidden>
      <div class="nemo-head"><b>What NVIDIA Nemotron sees</b><span id="nemoTime"></span></div>
      <div class="nemo-what" id="nemoWhat"></div>
      <div class="nemo-row" id="nemoHands"></div>
      <div class="nemo-row" id="nemoSign"></div>
      <ul class="hist" id="nemoHist"></ul>
    </section>
    <div class="badges">
      <div class="edge" id="thermo" hidden></div>
      <div class="edge" id="edge">On-device AI: starting...</div>
    </div>
    <button class="menu-btn" id="menuBtn" type="button" aria-expanded="false" aria-controls="drawer">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M18 11V6a2 2 0 0 0-4 0v4"/><path d="M14 10V4a2 2 0 0 0-4 0v6"/>
        <path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"/>
      </svg>
      <span>Hand signs</span>
    </button>
  </div>
  <div class="backdrop" id="backdrop"></div>
  <aside class="drawer" id="drawer" hidden>
    <div class="drawer-head">
      <h2>Nemotron hand signs</h2>
      <button class="close-btn" id="closeBtn" type="button" aria-label="Close menu">&times;</button>
    </div>
    <div class="drawer-body">
      <p class="tip">Face the camera and <b>hold the sign steady</b> until the car speaks (~4–6 s normally; 1 check is enough when the shared Nemotron cloud is busy). Tap any card to enlarge.</p>
      <button class="sign" type="button">
        <img src="/icons/all_good.png" alt="Thumbs up">
        <div class="sign-info">
          <div class="sign-name">All good</div>
          <div class="sign-how">One hand, thumbs up, held steady.</div>
          <div class="sign-says">“All good back here.”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/need_break.png" alt="Two hands forming a T">
        <div class="sign-info">
          <div class="sign-name">Need a break</div>
          <div class="sign-how">Both hands make a T (time-out): one flat hand horizontal, the other upright touching it.</div>
          <div class="sign-says">“Can we stop for a break soon?”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/volume_down.png" alt="Flat hand palm down pushing downward">
        <div class="sign-info">
          <div class="sign-name">Volume down</div>
          <div class="sign-how">One flat hand, palm facing down, push downward.</div>
          <div class="sign-says">“Could you turn the volume down, please?”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/volume_up.png" alt="Flat hand palm up lifting upward">
        <div class="sign-info">
          <div class="sign-name">Volume up</div>
          <div class="sign-how">One flat hand, palm facing up, lift upward.</div>
          <div class="sign-says">“Could you turn the volume up, please?”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/slower.png" alt="Open palm facing camera like a stop sign">
        <div class="sign-info">
          <div class="sign-name">Slower</div>
          <div class="sign-how">One flat hand, palm toward the camera, fingers up, held still like a stop sign.</div>
          <div class="sign-says">“Could you slow down, please?”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/faster.png" alt="Finger or hand circling forward">
        <div class="sign-info">
          <div class="sign-name">Faster</div>
          <div class="sign-how">Raise one index finger and circle it (or hold it pointing up).</div>
          <div class="sign-says">“Could you go a bit faster?”</div>
        </div>
      </button>
      <button class="sign" type="button">
        <img src="/icons/thank_you.png" alt="Flat hand from chin moving forward and down">
        <div class="sign-info">
          <div class="sign-name">Thank you</div>
          <div class="sign-how">Flat hand at your chin moving forward &amp; down (or press both palms together).</div>
          <div class="sign-says">“Thank you!”</div>
        </div>
      </button>
      <p class="note">Slow, held signs work best — fast movement blurs on camera. Waving, talking with your hands, touching hair/glasses, or resting your chin on a curled hand are ignored.</p>
    </div>
  </aside>
<script>
  var btn = document.getElementById("menuBtn"), drawer = document.getElementById("drawer"),
      backdrop = document.getElementById("backdrop");
  function setOpen(open) {
    drawer.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    backdrop.className = open ? "backdrop open" : "backdrop";
    try { localStorage.setItem("sd_signs_open", open ? "1" : "0"); } catch (e) {}
  }
  btn.onclick = function () { setOpen(drawer.hidden); };
  document.getElementById("closeBtn").onclick = backdrop.onclick = function () { setOpen(false); };
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !drawer.hidden) setOpen(false); });
  document.querySelectorAll(".sign").forEach(function (el) {
    el.onclick = function () { el.classList.toggle("zoom"); };
  });
  // People found by the Edge Impulse model running on this UNO Q, drawn over the live view.
  var feed = document.getElementById("feed"), cv = document.getElementById("boxes"), edge = document.getElementById("edge");
  function drawBoxes(o) {
    var r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    cv.width = r.width * dpr; cv.height = r.height * dpr;
    var g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, r.width, r.height);
    var iw = feed.naturalWidth || 4, ih = feed.naturalHeight || 3, k = Math.min(r.width / iw, r.height / ih);
    var w = iw * k, h = ih * k, ox = (r.width - w) / 2, oy = (r.height - h) / 2;   // object-fit: contain
    g.lineWidth = 3; g.font = "600 13px -apple-system, sans-serif";
    (o.boxes || []).forEach(function (b) {
      var sure = b.conf >= 0.5, x = ox + b.x * w, y = oy + b.y * h;
      g.strokeStyle = g.fillStyle = sure ? "#4ade80" : "rgba(250,204,21,.8)";
      g.strokeRect(x, y, b.w * w, b.h * h);
      g.fillText("person " + b.conf.toFixed(2), x + 4, Math.max(14, y - 5));
    });
  }
  function poll() {
    if (document.hidden) return;
    fetch("/occupancy", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (o) {
      drawBoxes(o);
      edge.innerHTML = o.people == null ? "On-device AI (Arduino UNO Q): " + (o.error || "paused")
        : "On-device AI (Arduino UNO Q): <b class='" + (o.cloud ? "cloud" : "local") + "'>" + o.people
          + (o.people === 1 ? " person" : " people") + "</b> \u00b7 " + (o.cloud ? "frames going to NVIDIA Nemotron" : "nothing sent to the cloud")
          + " \u00b7 detection " + o.ms + " ms on-device";
    }).catch(function () {});
  }
  setInterval(poll, 1000); poll();
  // Cabin climate from the Modulino Thermo, read by this board's microcontroller.
  var thermo = document.getElementById("thermo");
  function pollThermo() {
    if (document.hidden) return;
    fetch("/climate", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (c) {
      thermo.hidden = c.celsius == null;
      if (c.celsius == null) return;
      thermo.className = "edge" + (c.celsius >= 35 || c.celsius <= 0 ? " hot" : "");
      thermo.innerHTML = "\U0001F321\ufe0f Modulino Thermo: <b>" + c.celsius.toFixed(1) + " \u00b0C</b> \u00b7 " + c.humidity + " % humidity";
    }).catch(function () {});
  }
  setInterval(pollThermo, 2000); pollThermo();
  // What Nemotron sees: the Jetson keeps the cabin watch's recent verdicts (dash.py GET /nemotron).
  var HUB = "http://172.20.10.2:8092", nemoEl = document.getElementById("nemo");
  var LEVEL = {low: "none", medium: "watch", high: "danger"};
  // The chip shows the most serious thing in the verdict, not only the danger level: closed or covered
  // eyes and phone use are separate fields (voice.py acts on them itself) and leave the level at "none".
  function status(v) {
    var l = LEVEL[v.level] || "none";
    if (l === "danger") return ["danger", "DANGER"];
    if (v.neck === "around_throat") return ["danger", "THROAT"];
    if (v.eyes === "closed") return ["watch", "EYES CLOSED"];
    if (v.eyes === "covered") return ["watch", "EYES COVERED"];
    if (v.phone === "using") return ["watch", "PHONE"];
    if (v.neck === "hand_on_throat") return ["watch", "THROAT"];
    if (l === "watch") return ["watch", "WATCH"];
    return ["none", "OK"];
  }
  var SIGN = {volume_down: "volume down", volume_up: "volume up", slower: "slower", faster: "faster",
              thank_you: "thank you", need_break: "need a break", all_good: "all good"};
  var EYES = {open: "open", closed: "closed", covered: "covered", down: "looking down", not_visible: "not visible"};
  var NECK = {none: "clear", chin_or_face: "hand at chin or face", hand_on_throat: "hand on throat", around_throat: "hands around throat"};
  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function clock(t) { var d = new Date(t * 1000); return d.toTimeString().slice(0, 8); }
  function pollNemo() {
    if (document.hidden) return;
    fetch(HUB + "/nemotron", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (n) {
      nemoEl.hidden = false;
      var v = n.recent[0], age = v ? n.now - v.at : 1e9, st = v ? status(v) : ["none", "OK"];
      var paused = n.camera === "dark" ? "Camera can't see (dark or covered): Nemotron paused"
                 : n.camera === "off" ? "No camera frames: Nemotron paused" : "";
      nemoEl.className = "nemo" + (paused || age > 20 ? " stale" : "");
      document.getElementById("nemoTime").textContent = v ? (v.took_s + " s · " + Math.round(age) + " s ago") : "";
      document.getElementById("nemoWhat").innerHTML = paused ? esc(paused) : !v ? "Waiting for the first verdict…"
        : "<span class='lvl " + st[0] + "'>" + st[1] + "</span>" + esc(v.what || "(nothing to report)");
      document.getElementById("nemoHands").innerHTML = v && !paused ? (v.neck ? "Neck: <b>" + esc(NECK[v.neck] || v.neck) + "</b> · " : "") + (v.eyes ? "Eyes: <b" + ((v.eyes === "closed" || v.eyes === "covered") ? " style='color:#fbbf24'" : "") + ">" + esc(EYES[v.eyes] || v.eyes) + "</b> · " : "")
        + (v.phone && v.phone !== "none" ? "Phone: <b" + (v.phone === "using" ? " style='color:#fbbf24'" : "") + ">" + esc(v.phone) + "</b> · " : "") + "Hands: <b>" + esc(v.hands || "none") + "</b> · People: <b>" + esc(v.people) + "</b>" : "";
      document.getElementById("nemoSign").innerHTML = v && !paused ? "Sign: <b>" + esc(SIGN[v.instruction] || "none") + "</b>"
        + (v.said ? " → <span class='said'>car said “" + esc(v.said) + "”</span>" : "")
        + " · " + n.per_min + " verdicts/min, median " + (n.median_s == null ? "-" : n.median_s + " s") : "";
      document.getElementById("nemoHist").innerHTML = n.recent.slice(1, 6).map(function (h) {
        var hs = status(h), cls = hs[0] === "danger" ? "d" : hs[0] === "watch" ? "w" : h.said ? "s" : "";
        return "<li class='" + cls + "'><span>" + clock(h.at) + "</span><span>" + hs[1] + "</span><span>"
          + esc(SIGN[h.instruction] || "-") + "</span><span>" + h.took_s + " s</span><span>" + esc(h.what) + "</span></li>";
      }).join("");
    }).catch(function () { nemoEl.hidden = true; });
  }
  setInterval(pollNemo, 1000); pollNemo();
  document.addEventListener("visibilitychange", function () {
    document.getElementById("feed").src = document.hidden ? "" : "/preview.mjpg";
  });
  try { if (localStorage.getItem("sd_signs_open") === "1") setOpen(true); } catch (e) {}
</script>
</body></html>
""".encode()

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
        elif path in ("/stream.mjpg", "/preview.mjpg"):
            preview = path == "/preview.mjpg"
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
                    self.wfile.flush()
                    if preview: time.sleep(0.4)   # ~2.5 fps for browsers: spares 2.4 GHz Wi-Fi for speaker audio
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/health":
            with cond: ns = latest["ns"]
            body = json.dumps({**stats, "width": WIDTH, "height": HEIGHT,
                               "last_frame_age_s": round((time.time_ns() - ns) / 1e9, 2) if ns else None}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path == "/occupancy":
            q = self.path.partition("?")[2]
            if "cloud=" in q: cloud.update(active=q.split("cloud=")[1][:1] == "1", at=time.time())
            body = json.dumps(occupancy_json()).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path == "/climate":
            fresh = climate["celsius"] is not None and time.time() - climate["at"] < 30
            body = json.dumps({"celsius": climate["celsius"] if fresh else None,
                               "humidity": climate["humidity"] if fresh else None,
                               "age_s": round(time.time() - climate["at"], 1) if climate["at"] else None,
                               "error": None if fresh else (climate["error"] or "reading too old")}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path.startswith("/icons/") and path[7:] in ICONS:
            img = ICONS[path[7:]]
            self.send_response(200); self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(img))); self.end_headers(); self.wfile.write(img)
        elif path == "/":
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(PAGE))); self.end_headers(); self.wfile.write(PAGE)
        else:
            self.send_error(404)

threading.Thread(target=capture_loop, daemon=True).start()
threading.Thread(target=climate_loop, daemon=True).start()
threading.Thread(target=occupancy_loop, daemon=True).start()
threading.Thread(target=matrix_loop, daemon=True).start()
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
