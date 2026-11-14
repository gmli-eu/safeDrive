# SafeDrive hub (runs on the Jetson). Scenario: hold the Atech port-6 button for 5 s
# -> Telegram message + live location that follows the phone (OwnTracks) for LIVE_PERIOD s.
import json, os, pathlib, socket, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
import paho.mqtt.client as mqtt

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
STATE_FILE = pathlib.Path(os.environ.get("STATE_DIR", "/data")) / "telegram.json"

lock = threading.Lock()
fix = None            # latest OwnTracks fix: dict(lat, lon, acc, tst)
live = None           # active live location: dict(msgs=[(token, chat, message_id)], until, last_edit)
state = {}

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def load_state():
    global state
    try: state = json.loads(STATE_FILE.read_text())
    except Exception: state = {}
    state.setdefault("chats", {})          # bot name -> [chat ids]

def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))

def recipients():
    return [(b["token"], c) for b in BOTS for c in state["chats"].get(b["name"], [])]

def tg(token, method, **params):
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=35) as r: body = json.load(r)
    except urllib.error.HTTPError as e: body = json.load(e)
    if not body.get("ok"): raise RuntimeError(f"{method}: {body.get('description')}")
    return body["result"]

def publish(topic, payload, retain=False):
    client.publish(topic, json.dumps(payload), qos=1, retain=retain)

def set_mode(mode): publish("car/hub/state", {"mode": mode}, retain=True)

# ---------- scenario 2: SOS long press ----------
def send_sos():
    global live
    try:
        targets = recipients()
        if not targets: raise RuntimeError("no Telegram chats linked yet: send any message to the bots")
        with lock: f = dict(fix) if fix else None
        has_fix = bool(f) and time.time() - f["tst"] <= MAX_FIX_AGE
        if has_fix:
            maps = f"https://maps.google.com/?q={f['lat']:.6f},{f['lon']:.6f}"
            text = (f"🚨 SafeDrive alert: the driver held the emergency button.\n"
                    f"Live location below (updates for {LIVE_PERIOD // 60} min).\n{maps}")
        else:
            text = ("🚨 SafeDrive alert: the driver held the emergency button.\n"
                    "⚠️ No recent GPS fix from the phone (is OwnTracks running?).")
        msgs, errors = [], []
        t0 = time.time()
        def send_one(target):
            token, chat = target
            try:
                tg(token, "sendMessage", chat_id=chat, text=text)
                log(f"SOS text to {chat} after {time.time() - t0:.1f}s")
                if has_fix:
                    m = tg(token, "sendLocation", chat_id=chat, latitude=f["lat"], longitude=f["lon"],
                           live_period=LIVE_PERIOD, horizontal_accuracy=min(f.get("acc") or 0, 1500) or None)
                    with lock: msgs.append((token, chat, m["message_id"]))
                    log(f"SOS location to {chat} after {time.time() - t0:.1f}s")
            except Exception as e:
                with lock: errors.append(str(e))
        # Every recipient at once, so the last chat doesn't wait for all the others.
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            list(pool.map(send_one, targets))
        log(f"SOS sent to {len(targets) - len(errors)}/{len(targets)} chats in {time.time() - t0:.1f}s", errors or "")
        if msgs:
            with lock: live = {"msgs": msgs, "until": time.time() + LIVE_PERIOD, "last_edit": 0}
        if len(errors) == len(targets): raise RuntimeError("; ".join(errors))
        if not has_fix: raise RuntimeError("alert sent without location (no recent phone fix)")
        publish("car/atech/notify", {"status": "sent"})
        set_mode("idle")
    except Exception as e:
        log("SOS failed:", e)
        publish("car/atech/notify", {"status": "error", "reason": str(e)})
        set_mode("error")

def update_live():
    global live
    with lock:
        if not live or not fix: return
        if time.time() > live["until"]: live = None; return
        if time.time() - live["last_edit"] < 5: return     # Telegram rate limit
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
    global fix
    try: data = json.loads(m.payload.decode() or "null")
    except Exception: data = m.payload.decode(errors="replace")
    if m.topic.startswith("owntracks/"):
        if isinstance(data, dict) and data.get("_type") == "location":
            with lock: fix = {"lat": data["lat"], "lon": data["lon"], "acc": data.get("acc"), "tst": data.get("tst", time.time())}
            log("phone location", data["lat"], data["lon"], "acc", data.get("acc"))
            threading.Thread(target=update_live, daemon=True).start()
        return
    if m.topic == "car/hub/state": return
    log("MQTT", m.topic, data)
    if m.topic == "car/atech/button" and isinstance(data, dict) and data.get("action") == "long_press" and data.get("id") == "sos":
        set_mode("alerting")
        threading.Thread(target=send_sos, daemon=True).start()

# ---------- Telegram: link chats, answer /where ----------
def telegram_loop(bot):
    name, token = bot["name"], bot["token"]
    try: log(f"Telegram bot {name}: @" + tg(token, "getMe")["username"])
    except Exception as e: log(f"Telegram bot {name} token problem:", e); return
    offset = None
    while True:
        try:
            for u in tg(token, "getUpdates", timeout=30, offset=offset, allowed_updates='["message"]'):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = msg.get("chat", {})
                if chat.get("type") != "private": continue
                cid = str(chat["id"])
                with lock:
                    chats = state["chats"].setdefault(name, [])
                    new = cid not in chats
                    if new: chats.append(cid); save_state()
                if new:
                    log(f"Telegram {name}: linked {chat.get('first_name')} ({cid})")
                    tg(token, "sendMessage", chat_id=cid, text="✅ SafeDrive connected. Emergency alerts from the car will come here.")
                elif (msg.get("text") or "").startswith("/where"):
                    with lock: f = dict(fix) if fix else None
                    if f: tg(token, "sendLocation", chat_id=cid, latitude=f["lat"], longitude=f["lon"])
                    else: tg(token, "sendMessage", chat_id=cid, text="No location from the phone yet.")
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
client.loop_forever()
