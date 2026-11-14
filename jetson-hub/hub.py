# SafeDrive hub (runs on the Jetson). Scenarios:
# - hold the Atech port-6 button for 2 s -> Telegram message + live location that follows
#   the phone (OwnTracks) for LIVE_PERIOD s.
# - "I'm ok" in the screen menu -> "I've arrived and everything is ok." + location to mom, dad or both.
# - crash or flip detected by the board's orientation sensor, and the driver doesn't answer
#   "ARE YOU OK?" within 10 s -> accident alert + live location to everyone.
# - voice.py asks for a spoken message to be sent (car/voice/send) -> Telegram text to mom, dad or both.
# - the cabin stays too hot or too cold (Modulino Thermo on the Uno Q) -> the car tells the driver
#   to turn on the AC (or the heating), and the safeDrive page shows a banner. Nothing to the contacts.
# - Nemotron (voice.py's cabin watch) sees danger to the driver (car/vision/threat) -> silent threat
#   alert + photo, clip and incident report to everyone. Nothing shows or sounds in the car.
# - the driver's eyes stay closed or covered for 10 s despite the car's spoken warnings
#   (car/vision/unresponsive) -> eyes-closed alert + photo, clip and incident report to everyone.
import base64, collections, json, math, os, pathlib, re, socket, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor
import paho.mqtt.client as mqtt

if os.environ.get("TZ") == "Europe/Helsinki" and not os.path.exists("/usr/share/zoneinfo/Europe/Helsinki"):
    os.environ["TZ"] = "EET-2EEST,M3.5.0/3,M10.5.0/4"; time.tzset()

# Try IPv4 before IPv6. On a phone hotspot IPv6 is often advertised but broken, and urllib
# then waits for the IPv6 attempt to time out before every Telegram call.
_getaddrinfo = socket.getaddrinfo
def _ipv4_first(*args, **kwargs):
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda ai: ai[0] != socket.AF_INET)
socket.getaddrinfo = _ipv4_first

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
# TELEGRAM_BOTS: JSON list of {"name": ..., "token": ...}. Everyone who messages a bot gets the alerts.
BOTS = json.loads(os.environ.get("TELEGRAM_BOTS") or "[]")
LIVE_PERIOD = int(os.environ.get("LIVE_PERIOD", "900"))      # seconds the live location stays live
MAX_FIX_AGE = int(os.environ.get("MAX_FIX_AGE", "1800"))     # ignore phone fixes older than this
# Emergency alerts (SOS, accident, threat, eyes closed) still send an older fix, up to this age, marked as the
# last known position: an old position is better than none.
LAST_KNOWN_MAX_AGE = int(os.environ.get("LAST_KNOWN_MAX_AGE", str(24 * 3600)))
FRESH_FIX_AGE = int(os.environ.get("FRESH_FIX_AGE", "60"))   # older than this: ask the phone for a new one
FIX_WAIT = float(os.environ.get("FIX_WAIT", "30"))           # check-in: how long a late answer from the phone still counts
MOVED_M = float(os.environ.get("MOVED_M", "100"))            # check-in: send the late answer only if it's this far off
SOS_FIX_WAIT = float(os.environ.get("SOS_FIX_WAIT", "60"))   # SOS with no location at all: keep waiting this long
EMERGENCY_NUMBER = os.environ.get("EMERGENCY_NUMBER", "112")  # named in the accident alert
# Camera (vision.py): photo + clip with every emergency alert.
VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:8091")
SOS_CLIP_BEFORE, SOS_CLIP_AFTER = 10, 5      # seconds around the SOS long press
ACCIDENT_COUNTDOWN = 10                      # the board's "ARE YOU OK?" countdown (ACCIDENT_COUNTDOWN_MS)
ACCIDENT_CLIP_BEFORE = 15                    # seconds before the impact
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
VERDICT_WINDOW = 60                          # seconds of Nemotron cabin verdicts kept in memory
# Cabin climate: the Uno Q reads its Modulino Thermo (unoq-camera/thermo/) and serves it as JSON.
CLIMATE_URL = os.environ.get("CLIMATE_URL", "http://172.20.10.4:8090/climate")
# People in the cabin, counted on the Uno Q by an Edge Impulse model (unoq-camera/stream.py).
OCCUPANCY_URL = os.environ.get("OCCUPANCY_URL", CLIMATE_URL.rsplit("/", 1)[0] + "/occupancy")
HOT_CAR_C = float(os.environ.get("HOT_CAR_C", "35"))     # at or above: too hot, the driver is asked to turn on the AC
COLD_CAR_C = float(os.environ.get("COLD_CAR_C", "0"))    # at or below: too cold, the heating
CABIN_HOLD_S = float(os.environ.get("CABIN_HOLD_S", "30"))   # it must stay that hot or cold this long
# A Telegram live location that's still being shared stays valid this long without an update
# (Telegram only sends a new position when the phone moves).
LIVE_SHARE_MAX_AGE = int(os.environ.get("LIVE_SHARE_MAX_AGE", str(6 * 3600)))
SHARE_HOWTO = ("To include your location in alerts, share your live location in this chat: "
               "📎 → Location → Share My Live Location → Until I turn it off.")
STATE_FILE = pathlib.Path(os.environ.get("STATE_DIR", "/data")) / "telegram.json"

lock = threading.Lock()
fix = None            # latest OwnTracks fix: dict(lat, lon, acc, tst)
live = None           # active live location: dict(msgs=[(token, chat, message_id)], until, last_edit)
state = {}
fix_arrived = threading.Event()   # set on every new phone fix (see wait_for_fix)
verdicts = collections.deque(maxlen=200)   # recent car/vision/nemotron dicts (last VERDICT_WINDOW s)
climate = {}          # newest cabin reading: dict(celsius, humidity, at)
occupancy = {}        # newest on-device person count: dict(people, empty_for_s, at)
nemotron_io = {"sent": 0.0, "answered": 0.0}   # when voice.py last sent frames to Nemotron / got a verdict

def cloud_active():
    # Frames are going to Nemotron: one was sent in the last 5 s, or a request is still waiting for
    # its answer. Shown on the UNO Q's LED matrix (cloud icon) and in green on the pages.
    now, io = time.time(), nemotron_io
    return now - io["sent"] < 5 or (io["sent"] > io["answered"] and now - io["sent"] < 300)

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def load_state():
    global state, fix
    try: state = json.loads(STATE_FILE.read_text())
    except Exception: state = {}
    state.setdefault("chats", {})          # bot name -> [chat ids]
    # Last phone fix, saved so a hub restart doesn't lose it (OwnTracks in Move mode
    # only reports again once the phone moves). recent_fix() still drops it once stale.
    fix = state.get("fix")

def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))

def recipients(names=None):
    # Every linked chat, or only the chats of the bots in names (e.g. {"mom"}).
    return [(b["token"], c) for b in BOTS if names is None or b["name"] in names
            for c in state["chats"].get(b["name"], [])]

def recent_fix():
    with lock: f = dict(fix) if fix else None
    if not f: return None
    sharing = f.get("live_until", 0) > time.time()   # Telegram live location still being shared
    return f if time.time() - f["tst"] <= (LIVE_SHARE_MAX_AGE if sharing else MAX_FIX_AGE) else None

def new_fix(f, source):
    # A new phone position, from OwnTracks or a Telegram live location.
    global fix
    with lock:
        fix = f
        state["fix"] = f
        save_state()
    fix_arrived.set()
    log(f"phone location ({source})", f["lat"], f["lon"], "acc", f.get("acc"))
    threading.Thread(target=update_live, daemon=True).start()

def fix_age(f): return time.time() - f["tst"]

def alert_fix():
    # For emergency alerts: the recent fix, or else the last known one (up to LAST_KNOWN_MAX_AGE),
    # marked "stale" so the message can say how old it is.
    f = recent_fix()
    if f: return f
    with lock: f = dict(fix) if fix else None
    if f and fix_age(f) <= LAST_KNOWN_MAX_AGE:
        f["stale"] = True
        return f
    return None

def age_text(seconds):
    m = seconds / 60
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h".replace(".0 h", " h")

def ask_phone():
    # Ask OwnTracks for a new fix (remote command reportLocation, enabled in the app).
    # False if the phone's topic isn't known yet (it's learned from its first location).
    with lock: topic = state.get("phone_topic")
    if not topic:
        log("can't ask the phone for a location: its OwnTracks topic isn't known yet")
        return False
    fix_arrived.clear()
    publish(topic + "/cmd", {"_type": "cmd", "action": "reportLocation"})
    log(f"asked the phone for a location on {topic}/cmd")
    return True

def wait_for_fix(timeout):
    if fix_arrived.wait(timeout):
        log("phone sent a new location")
        return recent_fix()
    log(f"no new location from the phone within {timeout:.0f}s")
    return None

def distance_m(a, b):
    # Haversine distance between two fixes, in metres.
    la1, la2 = math.radians(a["lat"]), math.radians(b["lat"])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def tg(token, method, **params):
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=35) as r: body = json.load(r)
    except urllib.error.HTTPError as e: body = json.load(e)
    if not body.get("ok"): raise RuntimeError(f"{method}: {body.get('description')}")
    return body["result"]

def tg_upload(token, method, field, filename, data, ctype, **params):
    # Like tg(), but multipart: sendPhoto / sendVideo with the file in the request.
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in params.items() if v is not None]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    # One try with a long timeout. A retry after a timeout can post the same photo or video twice:
    # over the hotspot the first upload often did arrive, just slowly.
    try:
        with urllib.request.urlopen(req, timeout=120) as r: res = json.load(r)
    except urllib.error.HTTPError as e: res = json.load(e)
    if not res.get("ok"): raise RuntimeError(f"{method}: {res.get('description')}")
    return res["result"]

# ---------- AI incident report (Gemini) ----------
# Two Gemini calls in parallel, so the report comes right after the clip:
#  - where_and_er(): Google Maps at the car's position -> street address + nearest emergency department.
#    It needs no clip, so it starts with the alert and is done by the time the clip is ready.
#  - describe_clip(): the cabin clip, the trigger and the Nemotron verdicts -> what is visible, how many
#    people, a sentence for the dispatcher. No tools, so it answers in JSON. It starts the moment
#    the clip is fetched, while the video is still uploading to Telegram.
# Thinking off (budget 0): the Maps call takes ~8 s instead of ~13 s with the same answer, the clip ~5 s.
def gemini(body, timeout=45):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}
    body.setdefault("generationConfig", {})["thinkingConfig"] = {"thinkingBudget": 0}
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r: res = json.load(r)
            break
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")[:240]
            if attempt == 0 and e.code == 400 and "thinking" in err.lower():
                body["generationConfig"].pop("thinkingConfig", None); continue   # a model that must think
            raise RuntimeError(f"HTTP {e.code}: {err}") from None
    cand = (res.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    tools = [p["toolCall"].get("toolType") or "?" for p in parts if "toolCall" in p]
    # Proof of grounding for the log: the places Google Maps returned that the answer is based on.
    places = [c["maps"].get("title") for c in (cand.get("groundingMetadata") or {}).get("groundingChunks") or [] if c.get("maps")]
    if places: tools.append(f"Maps sources: {'; '.join(t for t in places[:5] if t)}")
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")).strip()
    return re.sub(r"[*_#`]|\[\d+\]", "", text).strip(), tools

MAPS_SYSTEM = ("Using Google Maps at the car's position, answer in exactly two lines of plain text:\n"
               "WHERE: <street address or nearest landmark>\n"
               "ER: <nearest hospital with an emergency department>, <distance in km>\n"
               "Google Maps already knows the car's position: search with queries like 'building near me' and "
               "'hospital emergency department near me', never with coordinates. Write unknown when Maps gives no answer. "
               "Never invent places.")

def where_and_er(loc):
    t = time.time()
    text, tools = gemini({"systemInstruction": {"parts": [{"text": MAPS_SYSTEM}]},
                          "contents": [{"role": "user", "parts": [{"text": "Where is the car, and which emergency department is nearest?"}]}],
                          "tools": [{"google_maps": {}}],
                          "toolConfig": {"includeServerSideToolInvocations": True,   # so the log shows the Maps calls
                                         "retrievalConfig": {"latLng": {"latitude": loc["lat"], "longitude": loc["lon"]}}}})
    line = lambda k: (re.search(rf"^{k}:\s*(.+)$", text, re.M | re.I) or [None, ""])[1].strip()
    where, er = line("WHERE"), line("ER")
    unknown = lambda v: not v or v.lower().startswith("unknown")
    log(f"report: Maps answered in {time.time() - t:.1f}s (tools: {', '.join(tools) or 'none'}): {where!r} / {er!r}")
    return {"where": None if unknown(where) else where, "er": None if unknown(er) else er}

def street_address(loc):
    # The exact address at the phone's position, from OpenStreetMap (Nominatim, no key, ~0.5 s).
    # Gemini + Maps names a nearby place instead, and a different one from run to run, which is
    # wrong for a sentence read to the emergency operator. Only the coordinates are sent.
    t = time.time()
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{loc['lat']:.6f}", "lon": f"{loc['lon']:.6f}",
                                "zoom": 18, "addressdetails": 1, "accept-language": "en"})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{q}", headers={"User-Agent": "SafeDrive/1.0 (car safety demo)"})
    with urllib.request.urlopen(req, timeout=8) as r: d = json.load(r)
    a = d.get("address") or {}
    street = " ".join(x for x in (a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("path"), a.get("house_number")) if x)
    town = " ".join(x for x in (a.get("postcode"), a.get("city") or a.get("town") or a.get("village") or a.get("municipality")) if x)
    addr = ", ".join(x for x in (street, town) if x)
    name = d.get("name") or ""
    if name and addr and name not in addr: addr += f" ({name})"
    log(f"report: address from OpenStreetMap in {time.time() - t:.1f}s: {addr!r}")
    return addr or None

CLIP_SYSTEM = """You describe a short clip from a camera INSIDE a car, sent after an emergency alert, for the driver's family.
- The camera sees the people in the car, not the road. Never describe the road, other cars or the cause of a crash unless it's clearly visible through a window.
- An attacker in the back seat behind the driver may be hidden from the camera so only the driver appears in the frame: focus on what is happening to the driver (hands around the neck, a weapon, restraint, grabbing the controls), even if only one person is visible.
- Describe only what is visible: movement, whether the driver moves or responds, people, objects. Never diagnose injuries or guess at medical state ("the driver doesn't move in the last 5 s of the clip" is OK, "the driver is unconscious" is not).
- If the clip is dark, black, covered, blurry or missing, say so instead of guessing, and give people as "unknown" unless people are clearly visible (or the cabin monitor reported them when there is no clip).
- Short, calm, factual: the family reads this on a phone while scared.
JSON fields:
what: 1-2 short sentences on what the clip shows.
people: the number of people visible in the car, as digits, or "unknown".
dispatcher: one or two sentences for the emergency operator: what happened, how many people, whether the driver moves or responds. No location (it is added after)."""
CLIP_SCHEMA = {"type": "object", "properties": {"what": {"type": "string"}, "people": {"type": "string"}, "dispatcher": {"type": "string"}},
               "required": ["what", "people", "dispatcher"]}

def describe_clip(label, clip, detail, start, end, event_at, vlist):
    ref = event_at if event_at is not None else end
    if label == "SOS":
        trigger, ref_desc = "SOS: the driver held the emergency button.", "the SOS button press"
    elif label == "accident":
        why = {"crash": "crash: a jolt over 2.5 g", "flip": "flip: turned over 75° and stayed still for 3 s"}.get(detail, f"sensor trigger ({detail or 'unknown'})")
        trigger = f"Accident ({why}), and the driver didn't answer \"ARE YOU OK?\" on the car's screen within 10 s."
        ref_desc = f"the {detail if detail in ('crash', 'flip') else 'impact'} (the 10 s countdown ended at +{ACCIDENT_COUNTDOWN}s)"
    elif label == "unresponsive":
        trigger = (f"The driver's eyes stayed {detail} (seen by NVIDIA Nemotron), and the car's spoken warnings didn't change it:"
                   " they may have fallen asleep or passed out, or something or someone is covering their eyes.")
        ref_desc = "the alert"
    else:
        trigger, ref_desc = f"Cabin threat seen by NVIDIA Nemotron: {detail or 'unclear'}.", "the threat frame"
    verdict_lines = "\n".join(f"  {v['captured_at'] - ref:+.1f}s: level={v.get('level')} people={v.get('people')} "
                              f"neck={v.get('neck')} eyes={v.get('eyes')} phone={v.get('phone')} sign={v.get('instruction')} what={v.get('what')!r}" for v in vlist) or "  none in this window"
    video = (f"Cabin clip attached, {time.strftime('%H:%M:%S', time.localtime(start))}-{time.strftime('%H:%M:%S', time.localtime(end))}."
             if clip else "No cabin video is available (camera off or unavailable): say there is no video.")
    parts = [{"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(clip).decode()}}] if clip else []
    parts.append({"text": f"Trigger: {trigger}\nVideo: {video}\nCabin temperature: {climate_text() or 'unknown'}\n"
                          f"Cabin monitor verdicts (NVIDIA Nemotron, times relative to {ref_desc}):\n{verdict_lines}"})
    t = time.time()
    text, _ = gemini({"systemInstruction": {"parts": [{"text": CLIP_SYSTEM}]}, "contents": [{"role": "user", "parts": parts}],
                      "generationConfig": {"responseMimeType": "application/json", "responseSchema": CLIP_SCHEMA}})
    d = json.loads(text)
    log(f"report: clip described in {time.time() - t:.1f}s")
    return {k: str(d.get(k) or "").strip() for k in ("what", "people", "dispatcher")}

def send_report(label, targets, clip, detail, start, end, event_at, vlist, t0, loc, maps_thread, maps_box):
    # Best effort: every failure is only logged, the family never gets an error message.
    try:
        if not GEMINI_KEY: raise RuntimeError("no GEMINI_API_KEY")
        c = describe_clip(label, clip, detail, start, end, event_at, vlist)
        for th in maps_thread or []: th.join(max(0, 45 - (time.time() - t0)))
        where, er = maps_box.get("address") or maps_box.get("where"), maps_box.get("er")
        coords = f"{loc['lat']:.5f}, {loc['lon']:.5f}" if loc else None
        place = where or (f"near {coords} (no address from Google Maps)" if coords else "unknown (no GPS fix from the phone)")
        stale = f"last known position, {age_text(fix_age(loc))} old" if loc and loc.get("stale") else ""
        if stale: place += f" ({stale}: the car may have moved)"
        said = c["dispatcher"].rstrip(". ") + "."
        if where or coords: said += f" {'Last known location' if stale else 'Location'}: {where or coords}."
        call = f'If you call {EMERGENCY_NUMBER}, say: "{said}"'
        if label == "threat": call = "Don't call the driver. " + call
        report = "\n".join(["📋 AI incident report (Gemini, can be wrong: check the clip)",
                            f"What the clip shows: {c['what'] or 'not available'}",
                            f"People in the car: {c['people'] or 'unknown'}",
                            *([f"Cabin temperature: {climate_text()}"] if climate_text() else []),
                            f"Where: {place}",
                            f"Nearest emergency department: {er + ' (from Google Maps)' if er else 'unknown'}",
                            call])[:1000]
        log(f"{label} Gemini report ready after {time.time() - t0:.1f}s:\n{report}")
        errors = []
        def one(target):
            try: tg(target[0], "sendMessage", chat_id=target[1], text=report)
            except Exception as e:
                log(f"{label} report to {target[1]} failed:", e)
                with lock: errors.append(str(e))
        with ThreadPoolExecutor(max_workers=len(targets)) as pool: list(pool.map(one, targets))
        if len(errors) == len(targets): raise RuntimeError("; ".join(errors))
        log(f"{label} report sent ({len(report)} chars) after {time.time() - t0:.1f}s")
    except Exception as e:
        log(f"{label} report unavailable:", e)

def send_media(label, targets, start, end, detail=None, event_at=None):
    # Photo right away, then the clip of [start, end] and the Gemini report. The clip is fetched while
    # the photo uploads (a slow hotspot must not age frames out of vision.py's 40 s buffer). The report
    # is written while the clip uploads, so it may arrive before the video.
    def fetch(path, timeout):
        with urllib.request.urlopen(VISION_URL + path, timeout=timeout) as r: return r.read()
    def to_all(method, field, name, data, ctype, **params):
        def one(target):
            try: tg_upload(target[0], method, field, name, data, ctype, chat_id=target[1], **params)
            except Exception as e: log(f"{label} {field} to {target[1]} failed:", e)
        with ThreadPoolExecutor(max_workers=len(targets)) as pool: list(pool.map(one, targets))
    t0 = time.time()
    try:
        loc, maps_box, maps_thread = alert_fix(), {}, []
        if GEMINI_KEY and loc:
            # The address (OpenStreetMap, exact) and the nearest emergency department (Gemini + Google Maps)
            # in parallel; Gemini's own "where" is only used if OpenStreetMap has no address.
            def run_maps():
                try: maps_box.update(where_and_er(loc))
                except Exception as e: log(f"{label} report: Maps lookup failed:", e)
            def run_address():
                try: maps_box["address"] = street_address(loc)
                except Exception as e: log(f"{label} report: OpenStreetMap address failed:", e)
            maps_thread = [threading.Thread(target=fn, daemon=True) for fn in (run_maps, run_address)]
            for th in maps_thread: th.start()
        clip_box = [None, None]
        def grab_clip():
            # Fetch the clip, then start the report at once: it must not wait for the photo upload,
            # which can take 10 s or more over the hotspot.
            try:
                clip_box[0] = fetch(f"/clip.mp4?start={start:.3f}&end={end:.3f}", max(end - time.time(), 0) + 90)
                log(f"{label} clip ready ({len(clip_box[0]) // 1024} KB) after {time.time() - t0:.1f}s")
            except Exception as e:
                log(f"{label} clip unavailable:", e)
            with lock:
                now = time.time()
                while verdicts and now - verdicts[0]["captured_at"] > VERDICT_WINDOW: verdicts.popleft()
                vlist = [v for v in verdicts if start <= v["captured_at"] <= end]
            clip_box[1] = threading.Thread(target=send_report, daemon=True, args=(
                label, targets, clip_box[0], detail, start, end, event_at, vlist, t0, loc, maps_thread, maps_box))
            clip_box[1].start()
        ct = threading.Thread(target=grab_clip, daemon=True); ct.start()
        try:
            photo = fetch("/photo.jpg", 5)
            to_all("sendPhoto", "photo", "car.jpg", photo, "image/jpeg",
                   caption=f"📷 In the car, {time.strftime('%H:%M:%S')}")
            log(f"{label} photo sent ({len(photo) // 1024} KB) after {time.time() - t0:.1f}s")
        except Exception as e:
            log(f"{label} photo unavailable:", e)
        ct.join()
        clip, report_thread = clip_box
        if clip:
            to_all("sendVideo", "video", "car.mp4", clip, "video/mp4", supports_streaming="true",
                   caption=f"🎥 In the car, {time.strftime('%H:%M:%S', time.localtime(start))}"
                           f"–{time.strftime('%H:%M:%S', time.localtime(end))}")
            log(f"{label} clip sent ({len(clip) // 1024} KB) after {time.time() - t0:.1f}s")
        if report_thread: report_thread.join()
    finally:
        log(f"{label} media done after {time.time() - t0:.1f}s")   # tools/deploy.py waits for this line

def publish(topic, payload, retain=False):
    client.publish(topic, json.dumps(payload), qos=1, retain=retain)

def set_mode(mode): publish("car/hub/state", {"mode": mode}, retain=True)

# ---------- scenarios 2 and 4: SOS long press, accident ----------
def send_alert(label, head, tail, reply_topic, media=None, detail=None, event_at=None):
    # Text + live location to every linked chat. Goes out at once with the last fix; the phone
    # is asked for a new one at the same time. When it answers, the live location moves to it
    # (update_live). With no fix at all, the live location follows as soon as the phone
    # answers (up to SOS_FIX_WAIT s). The result goes to reply_topic.
    global live
    try:
        targets = recipients()
        if not targets: raise RuntimeError("no Telegram chats linked yet: send any message to the bots")
        f = alert_fix()
        asked = (not f or fix_age(f) > FRESH_FIX_AGE) and ask_phone()
        if f:
            maps = f"https://maps.google.com/?q={f['lat']:.6f},{f['lon']:.6f}"
            if f.get("stale"):
                when = (f"⚠️ Last known position, {age_text(fix_age(f))} ago: the phone hasn't sent a newer one, so the car "
                        "may have moved. The live location below moves as soon as the phone answers.\n")
                log(f"{label}: no recent fix, using the last known one ({age_text(fix_age(f))} old)")
            else:
                when = f"Last known position, {age_text(fix_age(f))} ago. " if fix_age(f) > FRESH_FIX_AGE else ""
            text = head + f"{when}Live location below (updates for {LIVE_PERIOD // 60} min).\n{maps}"
        elif asked:
            text = head + "📍 Getting the location from the phone..."
        else:
            text = head + "⚠️ No recent location from the phone (is its live location shared with a SafeDrive bot?)."
        text += tail
        msgs, errors = [], []
        t0 = time.time()
        def send_live(token, chat, f):
            m = tg(token, "sendLocation", chat_id=chat, latitude=f["lat"], longitude=f["lon"],
                   live_period=LIVE_PERIOD, horizontal_accuracy=min(f.get("acc") or 0, 1500) or None)
            with lock: msgs.append((token, chat, m["message_id"]))
            log(f"{label} location to {chat} after {time.time() - t0:.1f}s")
        def send_one(target):
            token, chat = target
            try:
                tg(token, "sendMessage", chat_id=chat, text=text)
                log(f"{label} text to {chat} after {time.time() - t0:.1f}s")
                if f: send_live(token, chat, f)
            except Exception as e:
                with lock: errors.append(str(e))
        # Every recipient at once, so the last chat doesn't wait for all the others.
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            list(pool.map(send_one, targets))
        log(f"{label} sent to {len(targets) - len(errors)}/{len(targets)} chats in {time.time() - t0:.1f}s", errors or "")
        if len(errors) == len(targets): raise RuntimeError("; ".join(errors))
        if media: threading.Thread(target=send_media, args=(label, targets, *media, detail, event_at), daemon=True).start()
        if not f and asked and (f := wait_for_fix(SOS_FIX_WAIT)):
            def follow_up(target):
                token, chat = target
                try: send_live(token, chat, f)
                except Exception as e: log(f"{label} location follow-up failed:", e)
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                list(pool.map(follow_up, targets))
        if msgs:
            with lock:
                live = {"msgs": msgs, "until": time.time() + LIVE_PERIOD, "last_edit": 0}
                moved = fix and fix["tst"] != f["tst"]
            if moved: threading.Thread(target=update_live, daemon=True).start()   # a fix that came in meanwhile
        if not msgs: raise RuntimeError("alert sent without location (no recent phone fix)")
        publish(reply_topic, {"status": "sent"})
        set_mode("idle")
    except Exception as e:
        log(f"{label} failed:", e)
        publish(reply_topic, {"status": "error", "reason": str(e)[:80].replace('"', "'")})
        set_mode("error")

def send_sos(at):
    send_alert("SOS", "🚨 SafeDrive alert: the driver held the emergency button.\n", "", "car/atech/notify",
               media=(at - SOS_CLIP_BEFORE, at + SOS_CLIP_AFTER), event_at=at)

ACCIDENT_KIND = {"crash": "a hard impact", "flip": "the car seems to have flipped or rolled over"}

def send_accident(kind, at):
    head = (f"🚨 SafeDrive: possible accident detected ({ACCIDENT_KIND.get(kind, 'unknown cause')}). "
            "The driver didn't answer \"ARE YOU OK?\" on the car's screen within 10 s.\n")
    tail = (f"\n\n👉 Call the driver now. If they don't answer, call emergency services "
            f"({EMERGENCY_NUMBER}) and give them this location.")
    # The board reports only after its 10 s countdown, so the impact was ~ACCIDENT_COUNTDOWN s ago:
    # the clip covers the run-up to the impact and the countdown after it.
    send_alert("accident", head, tail, "car/atech/accident/result",
               media=(at - ACCIDENT_COUNTDOWN - ACCIDENT_CLIP_BEFORE, at), detail=kind, event_at=at - ACCIDENT_COUNTDOWN)

# Threat seen by the camera (voice.py's Nemotron loop publishes car/vision/threat). Silent in the car:
# nothing on the screen or speaker (car/vision/threat/result has no subscriber), so whoever is
# threatening the driver doesn't learn that an alert went out.
THREAT_LEVELS = {"high"}                      # other levels are only logged
THREAT_COOLDOWN = 120                         # seconds: one alert per incident, not one per camera frame
THREAT_CLIP_BEFORE, THREAT_CLIP_AFTER = 10, 5
last_threat = 0.0

def send_threat(what, at):
    head = (f"🚨 SafeDrive: the car's camera may have seen a threat to the driver: {what or 'unclear'}.\n"
            "This is automatic image recognition and can be wrong: check the photo and clip below.\n")
    tail = ("\n\n👉 Don't call the driver: a ringing phone could put them at risk. If the photo confirms it, "
            f"call emergency services ({EMERGENCY_NUMBER}) and give them this location.")
    send_alert("threat", head, tail, "car/vision/threat/result",
               media=(at - THREAT_CLIP_BEFORE, at + THREAT_CLIP_AFTER), detail=what, event_at=at)

# Driver's eyes closed for EYES_ALERT_S in voice.py, despite the car's spoken warnings (car/vision/unresponsive).
# Not silent: the car has already spoken, and calling the driver may wake them.
UNRESPONSIVE_COOLDOWN = 120
UNRESPONSIVE_CLIP_BEFORE, UNRESPONSIVE_CLIP_AFTER = 15, 5
last_unresponsive = 0.0

def send_unresponsive(closed_s, how, at):
    # how: "closed" or "covered" (hands, an object or a hood over the eyes), as Nemotron last saw it
    why = ("They may have fallen asleep or passed out." if how == "closed" else
           "Something is over their eyes (hands, an object or a hood): they can't see the road, or someone is covering them.")
    head = (f"🚨 SafeDrive: the driver's eyes have been {how} for {closed_s:.0f} s, and they didn't react when the car "
            f"spoke to them. {why}\n"
            "This is automatic image recognition and can be wrong: check the photo and clip below.\n")
    tail = ("\n\n👉 Call the driver now: a ringing phone may wake them. If they don't answer, call emergency services "
            f"({EMERGENCY_NUMBER}) and give them this location.")
    send_alert("unresponsive", head, tail, "car/vision/unresponsive/result",
               media=(at - UNRESPONSIVE_CLIP_BEFORE, at + UNRESPONSIVE_CLIP_AFTER), detail=f"{how} for {closed_s:.0f} s", event_at=at)

# ---------- too hot or too cold inside: Modulino Thermo ----------
# Heat (and cold) wears on a driver's attention, and hits children, older passengers and pets hardest.
# When the cabin stays at or above HOT_CAR_C (or at or below COLD_CAR_C) for CABIN_HOLD_S, the car
# says it once (voice.py) and the safeDrive page shows a banner while it lasts. car/cabin/comfort is
# retained: {"state": "hot" | "cold" | "ok", "celsius": ...}. Only the people in the car are told,
# and only while someone is in it: the Uno Q's on-device person detection decides (someone seen in
# the last OCCUPIED_WINDOW s). If it can't tell (detector down, dark cabin), the warning goes out anyway.
# While it lasts and someone is inside, the warning is repeated every CABIN_REMIND_S ("reminder": true).
CABIN_REMIND_S = float(os.environ.get("CABIN_REMIND_S", "300"))
cabin = {"kind": None, "since": 0.0, "announced": None, "announced_at": 0.0}

def climate_text():
    c = climate
    if c.get("celsius") is None or time.time() - c.get("at", 0) > 120: return ""
    return f"{c['celsius']:.1f} °C, {c['humidity']:.0f} % humidity (Modulino Thermo)"

OCCUPIED_WINDOW = 60

def occupied():
    o = occupancy
    if o.get("people") is None or time.time() - o.get("at", 0) > 15: return True   # can't tell: assume someone
    return o["people"] > 0 or o["empty_for_s"] + (time.time() - o["at"]) < OCCUPIED_WINDOW

def check_cabin(celsius, humidity):
    now = time.time()
    kind = "hot" if celsius >= HOT_CAR_C else "cold" if celsius <= COLD_CAR_C else None
    # 1 °C of hysteresis, so a reading wobbling around the limit doesn't warn again and again.
    if cabin["kind"] == "hot" and celsius > HOT_CAR_C - 1: kind = "hot"
    if cabin["kind"] == "cold" and celsius < COLD_CAR_C + 1: kind = "cold"
    if kind != cabin["kind"]:
        log(f"cabin {kind or 'back to normal'}: {celsius:.1f} °C, {humidity:.0f} %")
        cabin.update(kind=kind, since=now)
    want = (kind if now - cabin["since"] >= CABIN_HOLD_S and occupied() else None) if kind else "ok"
    if want and want != cabin["announced"]:
        cabin.update(announced=want, announced_at=now)
        publish("car/cabin/comfort", {"state": want, "celsius": celsius, "at": round(now)}, retain=True)
    elif want in ("hot", "cold") and want == cabin["announced"] and now - cabin["announced_at"] >= CABIN_REMIND_S:
        cabin["announced_at"] = now
        log(f"cabin still {want}: {celsius:.1f} °C, reminding")
        publish("car/cabin/comfort", {"state": want, "celsius": celsius, "at": round(now), "reminder": True}, retain=True)

def occupancy_loop():
    # Every 2 s: the Uno Q's on-device count -> car/cabin/occupancy (voice.py pauses the cloud cabin
    # watch on it, the Gemini agent reads it). Not retained: it's only meaningful while fresh.
    last_people, last_err = -1, None
    while True:
        try:
            # ?cloud= tells the Uno Q whether frames are going to Nemotron (for its LED matrix).
            with urllib.request.urlopen(f"{OCCUPANCY_URL}?cloud={int(cloud_active())}", timeout=5) as r: d = json.load(r)
            people = d.get("people")
            occupancy.update(people=people, empty_for_s=float(d.get("empty_for_s") or 0), confidence=d.get("confidence"), at=time.time())
            publish("car/cabin/occupancy", {"people": people, "empty_for_s": occupancy["empty_for_s"],
                                            "confidence": d.get("confidence"), "error": d.get("error"),
                                            "cloud": cloud_active(), "at": round(time.time(), 1)})
            if people != last_people: log("cabin occupancy (on-device):", people if people is not None else d.get("error"))
            last_people, last_err = people, None
        except Exception as e:
            occupancy.update(people=None)
            if str(e) != last_err: log("cabin occupancy unavailable:", e)
            last_people, last_err = -1, str(e)
        time.sleep(2)

def climate_loop():
    # Polls the Uno Q every 5 s. car/cabin/climate (retained) feeds voice.py and the phone page.
    last_pub, last_err = None, None
    while True:
        try:
            with urllib.request.urlopen(CLIMATE_URL, timeout=5) as r: d = json.load(r)
            if d.get("celsius") is None: raise RuntimeError(d.get("error") or "no reading")
            c, h = float(d["celsius"]), float(d["humidity"])
            climate.update(celsius=c, humidity=h, at=time.time() - float(d.get("age_s") or 0))
            if last_err: log("cabin climate back:", c, "°C", h, "%")
            last_err = None
            if last_pub is None or (c, h) != last_pub[:2] or time.time() - last_pub[2] > 60:
                publish("car/cabin/climate", {"celsius": c, "humidity": h, "at": round(climate["at"])}, retain=True)
                last_pub = (c, h, time.time())
            check_cabin(c, h)
        except Exception as e:
            if str(e) != last_err: log("cabin climate unavailable:", e)
            last_err = str(e)
        time.sleep(5)

# ---------- scenario 3: "I'm ok" check-in from the screen menu ----------
CHECKIN_TO = {"mom": {"mom"}, "dad": {"dad"}, "both": {"mom", "dad"}}

def send_checkin(to):
    # Goes out at once with the last fix (up to MAX_FIX_AGE old). If that's over FRESH_FIX_AGE
    # old, the phone is asked for a new one in the background; a late answer (FIX_WAIT s)
    # is sent as a second pin only if it's over MOVED_M away, or if there was no fix at all.
    def result(**payload): publish("car/atech/checkin/result", payload)
    try:
        names = CHECKIN_TO.get(to)
        if not names: raise RuntimeError(f"unknown recipient {to!r}")
        targets = recipients(names)
        if not targets: raise RuntimeError(f"No chat linked for {to}: open the bot and tap Start")
        f = recent_fix()
        asked = (not f or fix_age(f) > FRESH_FIX_AGE) and ask_phone()
        text = "I've arrived and everything is ok."
        if f: text += f"\nhttps://maps.google.com/?q={f['lat']:.6f},{f['lon']:.6f}"
        elif asked: text += "\n📍 Getting the location from the phone..."
        else: text += "\n⚠️ No recent location from the phone (is its live location shared with a SafeDrive bot?)."
        errors = []
        def send_pin(token, chat, f):
            tg(token, "sendLocation", chat_id=chat, latitude=f["lat"], longitude=f["lon"],
               horizontal_accuracy=min(f.get("acc") or 0, 1500) or None)
        def send_one(target):
            token, chat = target
            try:
                tg(token, "sendMessage", chat_id=chat, text=text)
                if f: send_pin(token, chat, f)
            except Exception as e:
                with lock: errors.append(str(e))
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            list(pool.map(send_one, targets))
        log(f"check-in ({to}) sent to {len(targets) - len(errors)}/{len(targets)} chats", errors or "")
        if len(errors) == len(targets): raise RuntimeError("Telegram send failed")
        if not f: result(status="sent", warning="no location")
        else: result(status="sent")
        if asked:
            new = wait_for_fix(FIX_WAIT)
            if new and (not f or distance_m(f, new) > MOVED_M):
                log("check-in: sending the phone's new location")
                for token, chat in targets:
                    try:
                        if f: tg(token, "sendMessage", chat_id=chat, text="📍 Updated location:")
                        send_pin(token, chat, new)
                    except Exception as e: log("check-in location follow-up failed:", e)
    except Exception as e:
        log("check-in failed:", e)
        result(status="error", reason=str(e)[:80].replace('"', "'"))

# ---------- scenario 5: voice message ("hey car, send a message to mom, ...") ----------
def send_voice_message(req):
    # From voice.py: {"id", "to": ["mom"] | ["all"], "text"}. Plain text, no location.
    def result(**payload): publish("car/voice/send/result", {"id": req.get("id"), **payload})
    try:
        to = req.get("to") or []
        targets = recipients(None if "all" in to else set(to))
        if not targets: raise RuntimeError(f"No chat linked for {' and '.join(to)}: open the bot and tap Start")
        text = "💬 From the driver: " + str(req.get("text") or "").strip()
        errors = []
        def send_one(target):
            try: tg(target[0], "sendMessage", chat_id=target[1], text=text)
            except Exception as e:
                with lock: errors.append(str(e))
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            list(pool.map(send_one, targets))
        log(f"voice message to {to} sent to {len(targets) - len(errors)}/{len(targets)} chats", errors or "")
        if len(errors) == len(targets): raise RuntimeError("Telegram send failed")
        result(status="sent")
    except Exception as e:
        log("voice message failed:", e)
        result(status="error", reason=str(e)[:80])

def update_live():
    global live
    with lock:
        if not live or not fix: return
        if time.time() > live["until"]: live = None; return
        wait = live["last_edit"] + 5 - time.time()          # Telegram rate limit: one edit per 5 s
        if wait > 0:
            # Too soon: retry once the limit allows it, so the latest fix isn't dropped.
            if not live.get("retry"):
                live["retry"] = True
                t = threading.Timer(wait, update_live); t.daemon = True; t.start()
            return
        live["retry"] = False
        live["last_edit"] = time.time()
        msgs, f = list(live["msgs"]), dict(fix)
    for token, chat, mid in msgs:
        try:
            tg(token, "editMessageLiveLocation", chat_id=chat, message_id=mid, latitude=f["lat"], longitude=f["lon"])
        except Exception as e:
            if "not modified" not in str(e): log("live update failed:", e)
    log("live location updated", f["lat"], f["lon"])

# ---------- MQTT ----------
def on_connect(c, userdata, flags, rc, props=None):
    log("MQTT connected", rc)
    c.subscribe([("car/#", 1), ("owntracks/#", 1)])
    set_mode("idle")

def on_message(c, userdata, m):
    global fix, last_threat, last_unresponsive
    # voice.py's own traffic (audio is binary), and Nemotron's verdict every few seconds (logged by voice.py)
    if m.topic.startswith(("car/atech/voice/", "car/passenger/voice/")) or m.topic == "car/voice/send/result": return
    if m.topic == "car/vision/sending":
        nemotron_io["sent"] = time.time(); return
    if m.topic == "car/vision/nemotron":
        nemotron_io["answered"] = time.time()
        try:
            v = json.loads(m.payload.decode())
            ts = float(v["captured_at"])
        except Exception: return
        now = time.time()
        if now - ts <= VERDICT_WINDOW:
            with lock:
                verdicts.append({**v, "captured_at": ts})
                while verdicts and now - verdicts[0]["captured_at"] > VERDICT_WINDOW: verdicts.popleft()
        return
    try: data = json.loads(m.payload.decode() or "null")
    except Exception: data = m.payload.decode(errors="replace")
    if m.topic.startswith("owntracks/"):
        if isinstance(data, dict) and data.get("_type") == "location":
            # owntracks/<user>/<device>: where ask_phone() sends the phone commands
            if m.topic.count("/") == 2:
                with lock: state["phone_topic"] = m.topic
            new_fix({"lat": data["lat"], "lon": data["lon"], "acc": data.get("acc"),
                     "tst": data.get("tst", time.time())}, "OwnTracks")
        return
    if m.topic in ("car/hub/state", "car/cabin/climate", "car/cabin/comfort", "car/cabin/occupancy"): return   # our own
    log("MQTT", m.topic, data)
    if m.topic == "car/atech/button" and isinstance(data, dict) and data.get("action") == "long_press" and data.get("id") == "sos":
        set_mode("alerting")
        threading.Thread(target=send_sos, args=(time.time(),), daemon=True).start()
    elif m.topic == "car/atech/accident" and isinstance(data, dict):
        set_mode("alerting")
        threading.Thread(target=send_accident, args=(data.get("kind"), time.time()), daemon=True).start()
    elif m.topic == "car/vision/threat" and isinstance(data, dict):
        if data.get("level") not in THREAT_LEVELS: return
        if time.time() - last_threat < THREAT_COOLDOWN:
            log(f"threat ignored: last alert {time.time() - last_threat:.0f}s ago (cooldown {THREAT_COOLDOWN}s)"); return
        last_threat = time.time()
        try: at = min(float(data.get("captured_at") or 0), time.time()) or time.time()
        except (TypeError, ValueError): at = time.time()
        if time.time() - at > 30: at = time.time()   # a stale timestamp would ask for frames already dropped
        threading.Thread(target=send_threat, args=(str(data.get("what", ""))[:200], at), daemon=True).start()
    elif m.topic == "car/vision/unresponsive" and isinstance(data, dict):
        if time.time() - last_unresponsive < UNRESPONSIVE_COOLDOWN:
            log(f"eyes-closed alert ignored: last one {time.time() - last_unresponsive:.0f}s ago"); return
        last_unresponsive = time.time()
        try: at = min(float(data.get("captured_at") or 0), time.time()) or time.time()
        except (TypeError, ValueError): at = time.time()
        if time.time() - at > 30: at = time.time()
        set_mode("alerting")
        threading.Thread(target=send_unresponsive, args=(float(data.get("closed_s") or 0), "covered" if data.get("eyes") == "covered" else "closed", at), daemon=True).start()
    elif m.topic == "car/atech/checkin" and isinstance(data, dict):
        threading.Thread(target=send_checkin, args=(data.get("to"),), daemon=True).start()
    elif m.topic == "car/voice/send" and isinstance(data, dict):
        threading.Thread(target=send_voice_message, args=(data,), daemon=True).start()

# ---------- Telegram: link chats, answer /where, take live locations ----------
def telegram_location(name, token, cid, msg, edited):
    # A location shared in a chat linked to one of our bots. A live location arrives as
    # one message, then as edits of it while the phone moves.
    loc = msg["location"]
    period = loc.get("live_period")
    if period: live_until = float("inf") if period >= 0x7FFFFFFF else msg["date"] + period
    else: live_until = 0   # one-off pin, or a live location that was stopped
    new_fix({"lat": loc["latitude"], "lon": loc["longitude"], "acc": loc.get("horizontal_accuracy"),
             "tst": msg.get("edit_date") or msg["date"], "live_until": live_until},
            f"Telegram {name}{' live' if period else ''}")
    if edited: return
    if period:
        tg(token, "sendMessage", chat_id=cid, text="📍 Thanks, alerts will include this live location while you share it.")
    else:
        tg(token, "sendMessage", chat_id=cid, text="📍 Got it, but that's a one-time location. " + SHARE_HOWTO)

def telegram_loop(bot):
    name, token = bot["name"], bot["token"]
    while True:  # the hub can start before the Jetson has internet: keep trying until Telegram answers
        try: log(f"Telegram bot {name}: @" + tg(token, "getMe")["username"]); break
        except OSError as e: log(f"Telegram bot {name}: no internet yet, retrying:", e); time.sleep(10)
        except Exception as e: log(f"Telegram bot {name} token problem:", e); return
    offset = None
    while True:
        try:
            for u in tg(token, "getUpdates", timeout=30, offset=offset, allowed_updates='["message","edited_message"]'):
                offset = u["update_id"] + 1
                edited = "edited_message" in u
                msg = u.get("edited_message") or u.get("message") or {}
                chat = msg.get("chat", {})
                if chat.get("type") != "private": continue
                cid = str(chat["id"])
                with lock:
                    chats = state["chats"].setdefault(name, [])
                    new = cid not in chats and not edited
                    if new: chats.append(cid); save_state()
                    linked = cid in chats
                if new:
                    log(f"Telegram {name}: linked {chat.get('first_name')} ({cid})")
                    tg(token, "sendMessage", chat_id=cid, text="✅ SafeDrive connected. Emergency alerts from the car will come here.\n\n" + SHARE_HOWTO)
                if "location" in msg:
                    # Only from chats linked to one of the configured bots.
                    if linked: telegram_location(name, token, cid, msg, edited)
                elif edited or new:
                    pass
                elif (msg.get("text") or "").startswith("/where"):
                    with lock: f = dict(fix) if fix else None
                    if f: tg(token, "sendLocation", chat_id=cid, latitude=f["lat"], longitude=f["lon"])
                    else: tg(token, "sendMessage", chat_id=cid, text="No location from the phone yet. " + SHARE_HOWTO)
                else:
                    tg(token, "sendMessage", chat_id=cid, text="SafeDrive is watching. Send /where for the car's current location.")
        except Exception as e:
            log(f"Telegram {name} poll error:", e); time.sleep(5)

load_state()
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="car-hub")
client.on_connect = on_connect
client.on_message = on_message
while True:
    try: client.connect(MQTT_HOST, 1883, 30); break
    except Exception as e: log("waiting for broker:", e); time.sleep(2)
if not BOTS: log("TELEGRAM_BOTS not set: Telegram disabled")
for b in BOTS: threading.Thread(target=telegram_loop, args=(b,), daemon=True).start()
threading.Thread(target=climate_loop, daemon=True).start()
threading.Thread(target=occupancy_loop, daemon=True).start()
client.loop_forever()
