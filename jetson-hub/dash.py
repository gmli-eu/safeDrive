# SafeDrive page for the driver's phone (runs on the Jetson, next to hub.py).
#   Open http://172.20.10.2:8092/ on the phone (it's the hotspot, so it can always reach the Jetson)
#   and leave it on screen in the phone holder, with Auto-Lock set to Never (the page can't keep the
#   screen on itself: the Wake Lock API needs HTTPS).
#   voice.py --MQTT car/nav/destination (retained)--> this page --live push (SSE)--> phone
#   The page then shows a big "Go to ..." button. One tap opens Google Maps (or Waze) with the route.
# Nothing opens by itself: iOS only lets a page open another app from a tap.
import os, subprocess, sys, shutil

# paho-mqtt is pure Python: installed once into the data volume and run from there (same as hub.py).
SITE = "/data/site-paho-mqtt-2.1.0"
if not os.path.isdir(SITE):
    print("installing paho-mqtt (first start only)...", flush=True)
    shutil.rmtree(SITE + ".new", True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore", "--disable-pip-version-check",
                    "--cache-dir", "/data/pip", "--target", SITE + ".new", "paho-mqtt==2.1.0"], check=True)
    os.rename(SITE + ".new", SITE)
sys.path.insert(0, SITE)

import json, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8092"))
TOPICS = {"car/nav/destination": "destination", "car/vision/camera": "camera"}   # retained, so a new page gets them at once

def log(*a): print(time.strftime("%H:%M:%S"), "[dash]", *a, flush=True)

state = {"destination": None, "camera": None}
version = 0
changed = threading.Condition()

def on_connect(c, userdata, flags, rc, props=None):
    log("MQTT connected", rc)
    c.subscribe([(t, 1) for t in TOPICS])

def on_message(c, userdata, m):
    global version
    try: data = json.loads(m.payload.decode() or "null")
    except Exception: return
    with changed:
        state[TOPICS[m.topic]] = data
        version += 1
        changed.notify_all()
    if m.topic == "car/nav/destination" and data: log("destination:", data.get("name"), "|", data.get("address"))

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SafeDrive">
<meta name="theme-color" content="#0b0f14">
<title>SafeDrive</title>
<style>
  :root { --bg:#0b0f14; --card:#151b23; --fg:#e8edf2; --muted:#8a96a3; --accent:#2f80ed; --ok:#27ae60; --warn:#f2994a; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
    font:17px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  body { display:flex; flex-direction:column; padding:max(16px, env(safe-area-inset-top)) 16px max(16px, env(safe-area-inset-bottom)); gap:16px; }
  header { display:flex; align-items:center; justify-content:space-between; }
  h1 { font-size:20px; margin:0; letter-spacing:.02em; }
  .pill { font-size:13px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--warn); }
  .dot.on { background:var(--ok); }
  main { flex:1; display:flex; flex-direction:column; justify-content:center; gap:16px; }
  .idle { text-align:center; color:var(--muted); }
  .idle b { color:var(--fg); display:block; font-size:22px; margin-bottom:6px; }
  .card { background:var(--card); border-radius:20px; padding:20px; }
  .card .label { color:var(--muted); font-size:14px; text-transform:uppercase; letter-spacing:.08em; }
  .card .name { font-size:26px; font-weight:700; margin:6px 0 2px; overflow-wrap:anywhere; }
  .card .addr { color:var(--muted); overflow-wrap:anywhere; }
  .card .when { color:var(--muted); font-size:13px; margin-top:8px; }
  a.go { display:flex; align-items:center; justify-content:center; text-align:center; width:100%;
    min-height:110px; border-radius:22px; border:0; background:var(--accent); color:#fff; text-decoration:none;
    font-size:28px; font-weight:700; padding:16px; overflow-wrap:anywhere; }
  a.go:active { filter:brightness(.85); }
  a.alt { display:block; text-align:center; color:var(--accent); font-size:18px; padding:14px; text-decoration:none; }
  .hint { color:var(--muted); font-size:13px; text-align:center; }
  [hidden] { display:none !important; }
</style></head>
<body>
<header><h1>SafeDrive</h1><span class="pill"><span class="dot" id="dot"></span><span id="conn">connecting</span></span></header>
<main>
  <section id="idle" class="idle" hidden><b>Ready</b>Ask the car for directions, for example<br>"Hey car, directions to Kamppi".</section>
  <section id="dest" hidden>
    <div class="card"><div class="label">Directions</div><div class="name" id="dName"></div>
      <div class="addr" id="dAddr"></div><div class="when" id="dWhen"></div></div>
    <a class="go" id="go" href="#">Go</a>
    <a class="alt" id="waze" href="#">Open in Waze instead</a>
  </section>
</main>
<p class="hint" id="camera"></p>
<p class="hint">Keep the screen on: Settings \u2192 Display &amp; Brightness \u2192 Auto-Lock \u2192 Never</p>
<script>
var state = {};
function enc(s) { return encodeURIComponent(s || ""); }
function ago(t) {
  var s = Math.max(0, Math.round(Date.now() / 1000 - t));
  return s < 60 ? "just now" : s < 3600 ? Math.round(s / 60) + " min ago" : Math.round(s / 3600) + " h ago";
}
function render() {
  var d = state.destination;
  var fresh = d && d.name && (Date.now() / 1000 - (d.at || 0)) < 6 * 3600;
  document.getElementById("idle").hidden = fresh;
  document.getElementById("dest").hidden = !fresh;
  if (fresh) {
    var q = d.query || [d.name, d.address].filter(Boolean).join(", ");
    // Universal links: they open the Google Maps / Waze app when it's installed, the website otherwise.
    var gm = "https://www.google.com/maps/dir/?api=1&travelmode=driving&dir_action=navigate&destination=" + enc(q)
           + (d.place_id ? "&destination_place_id=" + enc(d.place_id) : "");
    document.getElementById("go").href = gm;
    document.getElementById("go").textContent = "Go to " + d.name + " →";
    document.getElementById("waze").href = "https://waze.com/ul?navigate=yes&q=" + enc(q);
    document.getElementById("dName").textContent = d.name;
    document.getElementById("dAddr").textContent = d.address || "";
    document.getElementById("dWhen").textContent = (d.heard ? "Heard \u201c" + d.heard + "\u201d \u00b7 " : "") + "Asked " + ago(d.at || 0);
  }
  var cam = state.camera && state.camera.state;
  document.getElementById("camera").textContent = cam === "ok" ? "" : cam === "dark" ? "Cabin camera can't see (dark or covered)" : cam === "off" ? "Cabin camera is off" : "";
}
render();
setInterval(render, 30000);

function connect() {
  var es = new EventSource("/events");
  es.onopen = function () { document.getElementById("dot").className = "dot on"; document.getElementById("conn").textContent = "car connected"; };
  es.onmessage = function (e) {
    var prev = state.destination && state.destination.at;
    state = JSON.parse(e.data); render();
    if (state.destination && state.destination.at !== prev && navigator.vibrate) navigator.vibrate(200);
  };
  es.onerror = function () {
    document.getElementById("dot").className = "dot"; document.getElementById("conn").textContent = "reconnecting";
    // EventSource retries by itself; after a long sleep in the background, iOS sometimes doesn't: start over.
    if (es.readyState === 2) { setTimeout(connect, 2000); }
  };
}
connect();
</script>
</body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                body = PAGE.encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            elif self.path == "/events":
                # Server-sent events: the whole state on connect and on every change, a comment every 15 s
                # so the hotspot doesn't drop an idle connection.
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store"); self.end_headers()
                seen = -1
                while True:
                    with changed:
                        if seen == version: changed.wait(15)
                        snap, v = json.dumps(state), version
                    if v != seen:
                        self.wfile.write(f"retry: 2000\ndata: {snap}\n\n".encode()); seen = v
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            elif self.path == "/health":
                body = json.dumps({"ok": True, **state}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="car-dash")
client.on_connect = on_connect
client.on_message = on_message
while True:
    try: client.connect(MQTT_HOST, 1883, 30); break
    except Exception as e: log("waiting for broker:", e); time.sleep(2)
client.loop_start()
ThreadingHTTPServer.daemon_threads = True
log(f"page on http://0.0.0.0:{PORT}/")
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
