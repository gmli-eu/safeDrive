# SafeDrive voice assistant (runs on the Jetson, next to hub.py).
# Press the port-5 button and talk: "hey car, send a message to mom, I'm running 5 min late",
# "hey car, ask Google what's the weather today", "hey car, turn on the light".
#   board mic --UDP port 1885--> speech-to-text (faster-whisper, local)
#   --> what to do (local LLM through Ollama; a few fixed phrases also work without it)
#   --> Telegram message (through hub.py) / Gemini (only for "ask Google") / light
#   --> spoken reply (Piper, local) --MQTT car/atech/voice/pcm--> board speaker
import os, subprocess, sys, shutil

# ---------- dependencies: installed once into the data volume, copied in on every start ----------
# The volume is mounted noexec, so native libraries can't load from it: keep the installed
# packages cached there and copy them into the container filesystem to use them.
PKGS = ["faster-whisper==1.2.1", "piper-tts==1.8.0", "soxr==1.1.0", "paho-mqtt==2.1.0", "pillow==11.3.0"]
CACHE_SITE = "/data/site-voice-2"          # bump the number when PKGS changes
RUN_SITE = "/opt/voice-site"
if not os.path.isdir(RUN_SITE):
    if os.path.isdir(CACHE_SITE):
        subprocess.run(["cp", "-a", CACHE_SITE, RUN_SITE], check=True)
    else:
        print("installing voice packages (first start only)...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore",
                        "--disable-pip-version-check", "--cache-dir", "/data/pip", "--target", RUN_SITE, *PKGS], check=True)
        shutil.rmtree(CACHE_SITE + ".new", True)
        subprocess.run(["cp", "-a", RUN_SITE, CACHE_SITE + ".new"], check=True)
        os.rename(CACHE_SITE + ".new", CACHE_SITE)
sys.path.insert(0, RUN_SITE)
os.environ.setdefault("HF_HOME", "/data/hf")
# Download models with plain Python HTTP (IPv4 first, see below), not hf_xet: xet makes its own
# connections, tries IPv6 first, and the hotspot's broken IPv6 left the Whisper download hanging.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import json, pathlib, queue, re, socket, threading, time, urllib.error, urllib.request, uuid
import numpy as np
import paho.mqtt.client as mqtt

# IPv4 first: on a phone hotspot IPv6 is often advertised but broken (same as hub.py).
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: sorted(_getaddrinfo(*a, **k), key=lambda ai: ai[0] != socket.AF_INET)

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma4:e2b")       # Gemma 4 E2B: the offline brain, on the Jetson GPU
# Gemma 4 can think before answering: off, a voice command must answer in about a second.
# (Only for Gemma: Ollama refuses "think" for models that can't think.)
NO_THINK = {"think": False} if LLM_MODEL.startswith("gemma4") else {}
OLLAMA_STORE = pathlib.Path(os.environ.get("OLLAMA_STORE", "/ollama/models"))   # Ollama's model volume
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Nemotron 3.5 Super VL (Verda endpoint) watches the in-car camera for danger near the driver.
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "https://containers.datacrunch.io/nemotron-3-5-super-vl-b300/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nemotron-3.5-super-vl")
VERDA_KEY = os.environ.get("VERDA_INFERENCE_KEY", "")
VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:8091")   # vision.py: newest camera frame
CONTACTS = json.loads(os.environ.get("CONTACTS") or "[]")      # Telegram bot names, e.g. ["mom", "dad"]
HUB_STATE = pathlib.Path(os.environ.get("HUB_STATE", "/hub/telegram.json"))   # hub.py's state: last phone fix
DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))

SR = 16000                 # board mic and speaker sample rate
PLAY_LEAD_S = 0.6          # how far ahead of the speaker the PCM is sent (board buffers 1.5 s)
PCM_CHUNK = 3200           # bytes per MQTT message: 100 ms
MIC_PORT = 1885            # the board sends mic audio here over UDP (see udp_loop)

def log(*a): print(time.strftime("%H:%M:%S"), "[voice]", *a, flush=True)

# ---------- models ----------
whisper = piper = vad = None
llm_ready = False

def load_models():
    global whisper, piper, vad
    from faster_whisper import WhisperModel
    from faster_whisper import vad as fw_vad
    from piper import PiperVoice
    t = time.time()
    whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
    log(f"speech-to-text ready ({WHISPER_MODEL}, {time.time() - t:.0f}s)")
    vad = fw_vad
    lang, region, name, quality = re.match(r"(\w+)_(\w+)-(\w+)-(\w+)", PIPER_VOICE).groups()
    vdir = DATA / "piper"; vdir.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{lang}_{region}/{name}/{quality}/{PIPER_VOICE}"
    for ext in (".onnx", ".onnx.json"):
        f = vdir / (PIPER_VOICE + ext)
        if not f.exists():
            log("downloading voice", f.name)
            urllib.request.urlretrieve(base + ext, str(f) + ".part"); os.rename(str(f) + ".part", f)
    piper = PiperVoice.load(vdir / (PIPER_VOICE + ".onnx"))
    log("text-to-speech ready", PIPER_VOICE)

def ollama(path, body, timeout=60):
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)

models_loaded = threading.Event()   # Whisper and Piper are up: the LLM download may start

def prepare_llm():
    # Fetch the model once (after Whisper, so the two don't share the hotspot), then keep it loaded.
    global llm_ready
    models_loaded.wait()
    while True:
        try:
            have = {m["name"] for m in ollama_get("/api/tags")["models"]}
            if LLM_MODEL not in have: fetch_llm()
            t = time.time()
            ollama("/api/generate", {"model": LLM_MODEL, "prompt": "hi", "stream": False, "keep_alive": -1, **NO_THINK, "options": {"num_predict": 1}}, timeout=600)
            llm_ready = True
            log(f"local LLM ready ({LLM_MODEL}, loaded in {time.time() - t:.0f}s)")
            return
        except Exception as e:
            log("local LLM not ready, retrying in 30 s:", repr(e)); time.sleep(30)

def fetch_llm():
    # Download the model from the Ollama registry here, straight into Ollama's model store (the
    # ollama-models volume, mounted at OLLAMA_STORE), exactly as Ollama's own pull would lay it
    # out: every layer and the config as blobs, then the manifest. Ollama's own pull goes over
    # IPv6, which the phone hotspot keeps resetting; Python here prefers IPv4. Resumable across
    # restarts, and every blob is checked against its sha256 before it's put in place.
    import hashlib
    name, _, tag = LLM_MODEL.partition(":")
    repo = name if "/" in name else "library/" + name
    reg = f"https://registry.ollama.ai/v2/{repo}"
    req = urllib.request.Request(f"{reg}/manifests/{tag or 'latest'}", headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    with urllib.request.urlopen(req, timeout=30) as r: manifest_raw = r.read()
    manifest = json.loads(manifest_raw)
    blobs = OLLAMA_STORE / "blobs"; blobs.mkdir(parents=True, exist_ok=True)
    total = sum(l["size"] for l in manifest["layers"]) + manifest["config"]["size"]
    log(f"downloading {LLM_MODEL}: {total / 1e9:.1f} GB (first start only)")
    got = 0
    for layer in [manifest["config"], *manifest["layers"]]:
        digest, size = layer["digest"], layer["size"]
        path = blobs / digest.replace(":", "-")
        if path.exists() and path.stat().st_size == size: got += size; continue
        part = path.with_name(path.name + ".voicepart")
        while (done := part.stat().st_size if part.exists() else 0) < size:
            try:
                rq = urllib.request.Request(f"{reg}/blobs/{digest}", headers={"Range": f"bytes={done}-"})
                with urllib.request.urlopen(rq, timeout=60) as r, open(part, "ab" if r.status == 206 else "wb") as f:
                    if r.status != 206: done = 0
                    mark = done
                    while (buf := r.read(1 << 20)):
                        f.write(buf); done += len(buf)
                        if done - mark >= 250_000_000: mark = done; log(f"LLM download {(got + done) * 100 // total}%")
            except OSError as e:
                log("LLM download interrupted, resuming in 10 s:", e); time.sleep(10)
        h = hashlib.sha256()
        with open(part, "rb") as f:
            while (buf := f.read(1 << 22)): h.update(buf)
        if "sha256:" + h.hexdigest() != digest:
            part.unlink(); raise RuntimeError(f"LLM download corrupt ({digest[:19]}), starting that part over")
        part.rename(path); got += size
    mdir = OLLAMA_STORE / "manifests" / "registry.ollama.ai" / repo
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / (tag or "latest")).write_bytes(manifest_raw)       # written last: Ollama sees the model only once complete
    log(f"{LLM_MODEL} installed")

def ollama_get(path):
    with urllib.request.urlopen(OLLAMA_URL + path, timeout=10) as r: return json.load(r)

# ---------- MQTT ----------
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="car-voice")
lock = threading.Lock()
session = None             # the current press: dict(id, audio bytearray, listening, started)
send_results = {}          # message id -> hub's reply
send_done = threading.Condition(lock)

def status(title, body="", color="fg", ms=8000):
    # Shown on the board's screen. ASCII only, no quotes (the board's parser is simple).
    clean = lambda s: re.sub(r'[^\x20-\x7e]', '', s.replace('"', "'").replace("\\", "/"))
    client.publish("car/atech/voice/status", json.dumps({"title": clean(title)[:13], "body": clean(body)[:200], "color": color, "ms": ms}))

def on_connect(c, userdata, flags, rc, props=None):
    log("MQTT connected", rc)
    c.subscribe([("car/atech/voice/#", 0), ("car/voice/send/result", 1)])

def on_message(c, userdata, m):
    global session
    if m.topic == "car/atech/voice/pcm": return            # our own output
    try: data = json.loads(m.payload.decode() or "null")
    except Exception: data = None
    if m.topic == "car/atech/voice/start":
        sid = (data or {}).get("sid")
        with lock:
            session = {"id": uuid.uuid4().hex[:8], "sid": sid, "chunks": early.pop(sid, {}), "listening": True, "started": time.time()}
            early.clear()
            s = session
        log("listening", s["id"])
        threading.Thread(target=listen, args=(s,), daemon=True).start()
    elif m.topic == "car/atech/voice/end":
        reason = (data or {}).get("reason")
        log("board stopped listening:", reason)
        with lock:
            if reason == "accident": session = None   # cut off mid-sentence: never act on half a request
            elif session: session["listening"] = False
    elif m.topic == "car/voice/send/result" and isinstance(data, dict):
        with lock:
            send_results[data.get("id")] = data
            send_done.notify_all()

def current(s):
    with lock: return session is s

# ---------- mic audio: UDP from the board ----------
# Each packet: session id (uint32 LE), sequence number (uint32 LE), then 16 kHz int16 PCM.
# UDP, not MQTT: when Wi-Fi is congested, a TCP write blocks the board's loop (the SOS button
# and the knob freeze); a UDP packet is just dropped. A lost packet becomes a short silence.
early = {}                 # packets that beat the MQTT start message: sid -> {seq: pcm}

def udp_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", MIC_PORT))
    while True:
        pkt = sock.recv(4096)
        if len(pkt) < 10: continue
        sid, seq = int.from_bytes(pkt[0:4], "little"), int.from_bytes(pkt[4:8], "little")
        with lock:
            if session and session["sid"] == sid:
                if seq < 2000: session["chunks"][seq] = pkt[8:]      # 64 s cap; stragglers after "end" still count
            elif len(early) < 4 and seq < 50:
                early.setdefault(sid, {})[seq] = pkt[8:]

def session_audio(s):
    # Packets in order; a missing one becomes silence of the same length. Call with lock held.
    ch = s["chunks"]
    if not ch: return b""
    size = len(next(iter(ch.values())))
    return b"".join(ch.get(i, bytes(size)) for i in range(max(ch) + 1))

# ---------- listening: decide when the driver has finished talking ----------
def to_float(pcm):
    a = np.frombuffer(bytes(pcm[: len(pcm) // 2 * 2]), dtype=np.int16).astype(np.float32) / 32768
    peak = float(np.abs(a).max()) if len(a) else 0
    return a * min(20.0, 0.5 / peak) if peak > 1e-4 else a   # the mic is quiet: bring speech up to a usable level

def listen(s):
    # The driver decides when they're done: release after a hold, or a second tap (the board
    # sends "end"). The board also gives up after 20 s; this is only a backstop if "end" is lost.
    while True:
        time.sleep(0.1)
        with lock:
            if session is not s: return
            if not s["listening"]: break
        if time.time() - s["started"] > 25:
            client.publish("car/atech/voice/stop", "{}"); break
    time.sleep(0.2)                                          # the last packets may still be on their way
    with lock:
        if session is not s: return
        s["listening"] = False
        pcm = session_audio(s)
    log(f"got {len(pcm) / 2 / SR:.1f}s of audio")
    try: handle(s, pcm)
    except Exception as e:
        log("voice request failed:", repr(e))
        if not current(s): return
        if whisper is None:   # first start: models still downloading or loading
            say(s, "I'm still starting up. Try again in a minute.", "Starting up", "Voice models are still loading. Try again in a minute.", "warn")
        else: say(s, "Sorry, something went wrong.", "Error", str(e)[:120], "alert")

# ---------- speech to text ----------
# "Hey car" isn't needed (the button is the wake word): strip any leading greeting, with or without
# "car". "Google" is never stripped: it decides whether a request goes online.
WAKE = re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay)\b\W*)?(?:(?:a\s+)?(?:car|cars|car's|kar|carl)\b\W*)?", re.I)

def transcribe(pcm):
    if whisper is None: raise RuntimeError("speech-to-text is still loading")
    audio = to_float(pcm)
    # No speech at all: don't transcribe. Whisper invents text from silence and noise
    # ("Thank you."), which must never turn into a message to someone.
    if vad and not vad.get_speech_timestamps(audio, vad.VadOptions(threshold=0.5)):
        log("no speech in the recording")
        return ""
    t = time.time()
    # Keep the prompt to the wake words: Whisper sometimes repeats its prompt on unclear audio.
    # The contact names as hints, so a short "Dad" isn't heard as "that" or "dead".
    hint = "Hey car." + "".join(f" {c.capitalize()}." for c in CONTACTS)
    segs, _ = whisper.transcribe(audio, language="en", beam_size=3, vad_filter=False, initial_prompt=hint)
    text = " ".join(x.text.strip() for x in segs).strip()
    log(f"heard in {time.time() - t:.1f}s: {text!r}")
    return fix_names(WAKE.sub("", text).strip())

# ---------- what does the driver want? ----------
ACTIONS = ["send_message", "ask_google", "directions", "light", "answer", "unclear"]
NAMES_GOOGLE = re.compile(r"\b(?:google|gemini)\b", re.I)   # the only way a request goes online
SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ACTIONS},
    "to": {"type": "array", "items": {"type": "string"}},
    "text": {"type": "string"}, "query": {"type": "string"}, "place": {"type": "string"},
    "on": {"type": "boolean"}, "reply": {"type": "string"}}, "required": ["action"]}

def system_prompt():
    who = ", ".join(CONTACTS) or "mom, dad"
    return f"""You turn what a driver says to their car into one JSON action. Reply with JSON only.
Actions:
- send_message: send a text message. "to" lists who gets it, from: {who}, or ["all"] for both/everyone/the family. "text" is the message itself, as the driver wants it sent (first person, without "send a message to ..." and without "hey car").
- ask_google: the driver says "Google" (or "Gemini") and asks it something, e.g. "ask Google what's the weather", "Google, find the nearest pharmacy and text mom I'm stopping there". Also use it for questions that need the internet, today's information or places (but not for going somewhere: that's directions) even without the word Google (the car will then tell the driver to say "ask Google"). "query" is the driver's whole request, as a complete sentence.
- directions: the driver wants to go somewhere or asks for directions or a route ("take me to Kamppi", "directions to Mannerheimintie 5", "navigate to the nearest petrol station"). "place" is only the destination, as said ("Kamppi", "Mannerheimintie 5", "nearest petrol station").
- light: turn the car's light on or off. "on" is true or false.
- answer: anything else you can answer yourself (simple facts, maths, jokes, small talk). "reply" is the spoken answer: one or two short sentences, no lists or symbols.
- unclear: empty, cut off, or not understandable."""

EXAMPLES = [
    ("send message to mom, I'm running 5 min late", {"action": "send_message", "to": ["mom"], "text": "I'm running 5 min late."}),
    ("tell mom and dad that I'm leaving the gym now", {"action": "send_message", "to": ["all"], "text": "I'm leaving the gym now."}),
    ("ask google what's the weather for today", {"action": "ask_google", "query": "What's the weather for today?"}),
    ("take me to the central railway station", {"action": "directions", "place": "central railway station"}),
    ("turn off the light", {"action": "light", "on": False}),
]

def understand(text):
    if not llm_ready:
        log("local LLM not ready: using the fixed phrases")
        return fallback(text)
    msgs = [{"role": "system", "content": system_prompt()}]
    for q, a in EXAMPLES:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(a)}]
    msgs.append({"role": "user", "content": text})
    t = time.time()
    try:
        r = ollama("/api/chat", {"model": LLM_MODEL, "messages": msgs, "format": SCHEMA, "stream": False, **NO_THINK,
                                 "keep_alive": -1, "options": {"temperature": 0, "num_predict": 200}}, timeout=30)
        intent = json.loads(r["message"]["content"])
    except Exception as e:
        log("local LLM failed, using the fixed phrases:", e)
        return fallback(text)
    log(f"understood in {time.time() - t:.1f}s:", intent)
    return intent

def fallback(text):
    # Works without the LLM (still downloading, or down): the common phrasings only.
    t = text.strip()
    m = re.match(r"(?:please\s+)?(?:send|sent|text|message|tell)\s+(?:a\s+)?(?:message\s+|text\s+)?(?:to\s+)?(\w+)(?:\s+and\s+(\w+))?\W*(?:that\s+|saying\s+)?(.+)", t, re.I)
    if m and all(w.lower() in (*CONTACTS, "both", "everyone", "all") for w in m.group(1, 2) if w):   # "tell me a joke" isn't a message
        to = [w.lower() for w in m.group(1, 2) if w]
        return {"action": "send_message", "to": ["all"] if {"both", "everyone", "all"} & set(to) else to, "text": m.group(3)}
    # "Google" anywhere ("ask Google", "let's google", Whisper mishears the verb), or anything
    # that needs today's internet: the rest of the sentence is the question.
    m = re.search(r"\b(?:google|gemini)\b\W*(?:for\s+|about\s+)?(.+)", t, re.I)
    if m: return {"action": "ask_google", "query": m.group(1)}
    if re.search(r"\b(?:weather|forecast|news|traffic|score)\b", t, re.I): return {"action": "ask_google", "query": t}   # the car then asks for "ask Google"
    m = re.search(r"\b(?:directions?|route|navigate|navigation|take me|drive me|get me)\s+(?:to|for)\s+(?:the\s+)?(.+)", t, re.I)
    if m: return {"action": "directions", "place": re.sub(r"[\s,]+please$", "", m.group(1).strip(" .?!"), flags=re.I)}
    m = re.search(r"\blights?\b.*\b(on|off)\b|\b(on|off)\b.*\blights?\b", t, re.I)
    if m: return {"action": "light", "on": (m.group(1) or m.group(2)).lower() == "on"}
    return {"action": "unclear"}

def local_answer(text):
    r = ollama("/api/chat", {"model": LLM_MODEL, "stream": False, "keep_alive": -1, **NO_THINK,
                             "options": {"temperature": 0.3, "num_predict": 120},
                             "messages": [{"role": "system", "content": "You are a car's voice assistant. Answer in one or two short spoken sentences. No lists or symbols."},
                                          {"role": "user", "content": text}]}, timeout=30)
    return r["message"]["content"].strip()

# ---------- actions ----------
def handle(s, pcm):
    status("Thinking...", "", "fg", 30000)
    text = transcribe(pcm)
    if not current(s): return
    if not text:
        return say(s, "Sorry, I didn't catch that.", "Sorry", "Didn't catch that. Press the button and try again.", "warn")
    status("Heard", text, "fg", 30000)
    if answer_who(s, text): return
    intent = understand(text)
    if not current(s): return
    act = intent.get("action")
    if act == "send_message": send_message(s, intent, text)
    elif act == "ask_google":
        # Online only when the driver asks for Google by name: everything else stays in the car,
        # whatever the local model decided.
        if NAMES_GOOGLE.search(text): ask_google(s, text)
        else: say(s, "I can't check that offline. Say 'ask Google' to look it up.", "Offline only", "Say 'ask Google ...' to look it up online.", "warn")
    elif act == "directions":
        # With "Google" named, Gemini finds the exact place with Maps first ("the nearest open pharmacy").
        # Otherwise only the phone page gets the destination: Google Maps looks it up when the driver taps Go.
        if NAMES_GOOGLE.search(text): ask_google(s, text)
        else: directions(s, (intent.get("place") or "").strip())
    elif act == "light":
        on = bool(intent.get("on"))
        client.publish("car/atech/light", json.dumps({"on": on}))
        say(s, f"Light {'on' if on else 'off'}.", f"Light {'on' if on else 'off'}", "", "ok")
    elif act == "answer":
        reply = (intent.get("reply") or "").strip() or local_answer(text)
        say(s, reply, "Answer", reply, "fg")
    else:
        say(s, "Sorry, I didn't understand. You can say: send a message to mom, or ask Google.", "Sorry", "Didn't understand: " + text, "warn")

# Whisper's usual mishearings of the contact names ("send dad" -> "send dead").
NAME_FIXES = [(re.compile(r"\b(send|sent|tell|text|message|to)\s+(?:a\s+message\s+to\s+)?dead\b", re.I), r"\1 dad"),
              (re.compile(r"\b(mum|mam|mother)\b", re.I), "mom"), (re.compile(r"\bfather\b", re.I), "dad")]
EVERYONE = re.compile(r"\b(?:both|every(?:one|body)|all|family|parents)\b", re.I)

def fix_names(text):
    for pat, rep in NAME_FIXES: text = pat.sub(rep, text)
    return text

def addressed_to_all(said):
    # Only if the driver said so: "both", "everyone", ... or named every contact.
    return bool(EVERYONE.search(said)) or (len(CONTACTS) > 1 and all(re.search(rf"\b{re.escape(c)}\b", said, re.I) for c in CONTACTS))

def resolve_contacts(to):
    to = [str(w).lower() for w in to or []]
    if any(w in ("all", "both", "everyone", "family") for w in to): return ["all"], []
    return to, [w for w in to if w not in CONTACTS]

def hub_send(to, msg):
    # Text to contacts through hub.py's Telegram bots. Returns (ok, reason).
    mid = uuid.uuid4().hex[:8]
    client.publish("car/voice/send", json.dumps({"id": mid, "to": to, "text": msg}), qos=1)
    with lock:
        send_done.wait_for(lambda: mid in send_results, timeout=40)
        r = send_results.pop(mid, None)
    if not r: return False, "the hub didn't answer, so it may not have gone out"
    return r.get("status") == "sent", r.get("reason") or ""

PENDING_S = 30
# Words that can surround the name in an answer ("send it to dad", "I said to dad", "just mom please").
WHO_FILLER = {"to", "and", "send", "sent", "it", "please", "the", "just", "i", "said", "say", "says", "of", "one", "oh", "uh", "um",
              "so", "my", "you", "can", "for", "message", "a", "him", "her", "them", "only", "yes", "yeah", "ok", "okay", "is", "s"}
# Only while the car waits for "Who should I send it to?": one-word answers that sound like a contact.
SOUNDS_LIKE = {"dead": "dad", "that": "dad", "dat": "dad", "daddy": "dad", "dud": "dad", "dan": "dad", "ted": "dad", "debt": "dad",
               "mum": "mom", "mam": "mom", "ma": "mom", "mama": "mom", "mommy": "mom", "mummy": "mom", "mother": "mom", "father": "dad"}
pending_message = {}       # a message waiting for its recipient: {"text", "until"}

def answer_who(s, said):
    # After "Who should I send it to?": if this press is only a name ("Dad", "mom and dad", "both"),
    # send the waiting message to them. Any other request cancels the waiting message.
    if not pending_message: return False
    msg, until = pending_message.get("text"), pending_message.get("until", 0)
    pending_message.clear()
    if time.time() > until: return False
    words = re.sub(r"[^a-z ]", " ", said.lower()).split()
    if len(words) > 7: return False                              # a whole new request, not an answer
    words = [SOUNDS_LIKE.get(w, w) for w in words if w not in WHO_FILLER]
    if not words or len(words) > 3: return False
    if all(w in ("both", "everyone", "all", "family") for w in words): to = ["all"]
    elif all(w in CONTACTS for w in words): to = ["all"] if set(words) >= set(CONTACTS) and len(CONTACTS) > 1 else sorted(set(words))
    else: return False
    send_message(s, {"to": to, "text": msg}, " ".join(words))
    return True

def send_message(s, intent, said):
    msg = (intent.get("text") or "").strip()
    to, unknown = resolve_contacts(intent.get("to"))
    if to == ["all"] and not addressed_to_all(said): to = []      # the model guessed "everyone": ask instead
    if not msg or not to or unknown:
        who = " or ".join(CONTACTS) or "mom or dad"
        if msg: pending_message.update(text=msg, until=time.time() + PENDING_S)   # the next press can just be a name
        return say(s, f"Who should I send it to? You can say {who}.", "Who to?", f"Contacts: {who}", "warn")
    names = "everyone" if to == ["all"] else " and ".join(to)
    status("Sending...", f"To {names}: {msg}", "fg", 45000)
    ok, reason = hub_send(to, msg)
    if ok: say(s, f"Sent to {names}.", "Sent", f"To {names}: {msg}", "ok")
    else: say(s, "I couldn't send it. " + reason, "Failed", reason, "alert")

def phone_location():
    # Last phone position the hub saved, if it's recent enough to mean "here".
    try: f = json.loads(HUB_STATE.read_text()).get("fix")
    except Exception: return None
    if not f: return None
    live = f.get("live_until", 0) > time.time()
    return f if time.time() - f["tst"] <= (6 * 3600 if live else 1800) else None

# ---------- local place names: "campy" -> "Kamppi" ----------
# Whisper writes a Finnish name the way an English speaker says it ("campy", "hacker nemi"). Match
# what it heard against the real place and street names around the car by how they sound: both
# sides go through sound_key(), which spells them the same way (c/k, y/i, double letters, ä/a...),
# then the closest name wins if it's close enough. Names: OpenStreetMap (Overpass API), fetched for
# the phone's area and cached; until then the bundled Helsinki list (places-helsinki.txt, no streets).
# Overpass only ever gets a map area, never what the driver said.
import difflib, unicodedata, zlib, base64 as _b64
SOUND_RULES = [(r"ph", "f"), (r"th", "t"), (r"sh", "s"), (r"ck", "k"), (r"ch", "k"), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"q", "k"),
               (r"x", "ks"), (r"ow", "au"), (r"w", "v"), (r"z", "s"), (r"j", "y"), (r"ee|ea|ie", "i"), (r"ou", "u"), (r"ei|ey", "ai"),
               (r"er\b", "a"), (r"y", "i"), (r"(?<=[^aeiou\s])h", ""), (r"h\b", ""), (r"(.)\1+", r"\1")]
# Words that are never part of a misheard name: "railway station" mustn't become one particular station.
COMMON_WORDS = set("""the a an to near me nearest closest station centre center street road mall airport hospital shopping square park
    harbour harbor port terminal market church museum beach university hotel please my home office work school railway train metro bus
    tram central city main north south east west old new big little petrol gas parking garage pharmacy shop store supermarket restaurant
    cafe bar gym library police of in at on for and go take drive get directions route way there here open now""".split())
PLACES_FILE = DATA / "places.json"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
PLACES_RADIUS_KM, STREETS_RADIUS_KM = 25, 12
places = []            # [(name, sound key)]

def sound_key(text):
    t = "".join(ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z ]", " ", t)
    for a, b in SOUND_RULES: t = re.sub(a, b, t)
    return t.replace(" ", "")

def set_places(names, source):
    global places
    places = [(n, sound_key(n)) for n in names if n and "(" not in n]
    log(f"{len(places)} place names ({source})")

PLACE_PREPOSITIONS = {"to", "near", "in", "at", "from", "around", "by", "past", "via"}

def fix_place(text, in_sentence=False):
    # -> (text with the misheard name replaced, what was heard, the name) or (text, None, None).
    # in_sentence: a whole request, not just the destination. Then only words right after "to", "near",
    # "in"... count, or ordinary words would turn into street names ("weather" -> Viittatie).
    words, best = text.split(), None
    for n in range(min(4, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            win = words[i:i + n]
            if in_sentence and (i == 0 or words[i - 1].lower() not in PLACE_PREPOSITIONS): continue
            if all(w.lower().strip(".,!?") in COMMON_WORDS for w in win): continue
            if not (re.search(r"[a-z]", win[0], re.I) and re.search(r"[a-z]", win[-1], re.I)): continue   # keep house numbers
            k = sound_key(" ".join(win))
            if len(k) < 3: continue
            for name, nk in places:
                if abs(len(nk) - len(k)) > max(3, len(k) // 2): continue
                r = difflib.SequenceMatcher(None, k, nk).ratio()
                if r < (0.9 if len(nk) <= 5 else 0.8): continue
                score = r + 0.05 * n                          # prefer covering more of the words
                if not best or score > best[0]: best = (score, i, n, name)
    if not best: return text, None, None
    _, i, n, name = best
    heard = " ".join(words[i:i + n])
    if heard.lower().strip(".,!?") == name.lower(): return text, None, None
    return " ".join(words[:i] + [name] + words[i + n:]), heard, name

def fetch_places(lat, lon):
    P, S = PLACES_RADIUS_KM * 1000, STREETS_RADIUS_KM * 1000
    around = lambda r: f"(around:{r},{lat:.3f},{lon:.3f})"
    q = (f'[out:csv(name,"name:en";false)][timeout:180];('
         f'nwr["place"~"^(city|town|village|suburb|quarter|neighbourhood|island|square)$"]["name"]{around(P)};'
         f'nwr["railway"="station"]["name"]{around(P)};nwr["public_transport"="station"]["name"]{around(P)};'
         f'nwr["shop"="mall"]["name"]{around(P)};nwr["amenity"~"^(hospital|university|college)$"]["name"]{around(P)};'
         f'nwr["tourism"~"^(attraction|museum)$"]["name"]{around(P)};nwr["aeroway"="aerodrome"]["name"]{around(P * 2)};'
         f'way["highway"~"^(trunk|primary|secondary|tertiary|residential|unclassified|pedestrian|living_street)$"]["name"]{around(S)};'
         f');out tags;')
    import urllib.parse
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": "SafeDrive/1.0"})
            with urllib.request.urlopen(req, timeout=240) as r: body = r.read().decode("utf-8", "replace")
            names = sorted({c.strip() for line in body.splitlines() for c in line.split("\t") if c.strip()})
            if len(names) > 50: return names
            log(f"place names: {url} sent only {len(names)}")
        except Exception as e:
            log(f"place names: {url} failed: {e!r}"[:200])
    return None

def km(a, b):
    import math
    dy = (a[0] - b[0]) * 111.2; dx = (a[1] - b[1]) * 111.2 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)

def places_loop():
    # Names for where the car is: the cache if it's for this area, otherwise fetch (about 2 min,
    # 0.5 MB). Checked every 5 min; a failed fetch is retried 10 min later.
    cache = None
    try: cache = json.loads(PLACES_FILE.read_text()); set_places(cache["names"], f"cached, {len(cache['names'])} for {cache['center']}")
    except Exception:
        if os.environ.get("PLACES_B64"):
            set_places(zlib.decompress(_b64.b64decode(os.environ["PLACES_B64"])).decode().splitlines(), "bundled Helsinki list")
    retry_at = 0
    while True:
        loc = phone_location()
        if loc and time.time() >= retry_at:
            here = (loc["lat"], loc["lon"])
            if not cache or km(here, cache["center"]) > 10 or time.time() - cache.get("at", 0) > 30 * 86400:
                log(f"fetching place names around {here[0]:.3f},{here[1]:.3f}...")
                names = fetch_places(*here)
                if names:
                    cache = {"center": [round(here[0], 3), round(here[1], 3)], "at": time.time(), "names": names}
                    PLACES_FILE.write_text(json.dumps(cache, ensure_ascii=False))
                    set_places(names, f"fetched for {cache['center']}")
                else:
                    retry_at = time.time() + 600
        time.sleep(300)

# ---------- directions: a "Go to ..." button on the SafeDrive page (dash.py) ----------
# The car never opens an app on the phone: iOS only allows that from a tap. The page on the phone
# shows the destination and one big button that opens Google Maps with the route.
def publish_destination(name, address="", query="", place_id="", by="car", heard=""):
    d = {"name": name[:80], "address": address[:160], "query": (query or ", ".join(x for x in (name, address) if x))[:200],
         "place_id": place_id, "by": by, "heard": heard[:80], "at": time.time()}
    client.publish("car/nav/destination", json.dumps(d), qos=1, retain=True)
    log("destination for the phone:", d)
    return d

def directions(s, place):
    if not place:
        return say(s, "Where to? Say for example: directions to Kamppi.", "Where to?", "Say: directions to ...", "warn")
    said = place
    place, heard, name = fix_place(place)
    if name: log(f"place name: heard {heard!r}, using {name!r}")
    # "near me" keeps a vague place ("petrol station") close by when Maps doesn't know where the phone is yet.
    query = re.sub(r"^(?:the\s+)?(?:nearest|closest)\s+(.+)", r"\1 near me", place, flags=re.I)
    publish_destination(place[:1].upper() + place[1:], "", query, by="car", heard=said if name else "")
    say(s, f"Directions to {place} are on your phone. Tap Go when it's safe.", "Directions",
        f"Tap Go on your phone: {place}", "ok")

# ---------- Gemini: the online co-pilot ----------
# One agent turn per request: Gemini gets Google Search and Google Maps grounding (with the car's
# position) plus the car's own functions. It may chain them ("find an open pharmacy and text mom
# I'm stopping there"); we run each function call and hand the result back until it answers.
GEMINI_FUNCTIONS = [
    {"name": "send_message", "description": "Send a text message from the driver to their contacts (via Telegram). Use the driver's own words in first person.",
     "parameters": {"type": "object", "properties": {
         "to": {"type": "array", "items": {"type": "string"}, "description": "Contact names, or [\"all\"] for everyone"},
         "text": {"type": "string"}}, "required": ["to", "text"]}},
    {"name": "set_light", "description": "Turn the car's cabin light on or off.",
     "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]}},
    {"name": "show_directions", "description": "Put a 'Go to <place>' button on the driver's phone (the SafeDrive page in the phone holder); one tap opens Google Maps with the route. Use it when the driver wants to go somewhere or asks for directions, after finding the exact place with Google Maps.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "The place's name as Google Maps lists it"},
         "address": {"type": "string", "description": "Its street address, with the city"}}, "required": ["name", "address"]}},
    {"name": "cabin_status", "description": "What the in-car camera monitor (NVIDIA Nemotron) saw most recently: danger level, description, number of people, back-seat hand sign.",
     "parameters": {"type": "object", "properties": {}}},
]
last_verdict = {}      # newest Nemotron verdict (watch_loop)
maps_places = []       # Google Maps places Gemini found in this request: [{"title", "placeId", "uri"}]

def gemini_call(contents, system, loc):
    tool_config = {"includeServerSideToolInvocations": True}
    if loc: tool_config["retrievalConfig"] = {"latLng": {"latitude": loc["lat"], "longitude": loc["lon"]}}
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "toolConfig": tool_config,
            "tools": [{"google_search": {}}, {"google_maps": {}}, {"function_declarations": GEMINI_FUNCTIONS}]}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=45) as r: return json.load(r)

def run_function(name, args, said=""):
    if name == "send_message":
        to, unknown = resolve_contacts(args.get("to"))
        if to == ["all"] and not addressed_to_all(said): return {"ok": False, "error": "the driver didn't say who: ask them which contact"}
        if not to or unknown: return {"ok": False, "error": f"unknown contact; known: {CONTACTS} or all"}
        status("Sending...", f"To {' and '.join(to)}: {args.get('text', '')}", "fg", 45000)
        ok, reason = hub_send(to, str(args.get("text", "")))
        return {"ok": ok, "error": reason} if not ok else {"ok": True, "sent_to": to}
    if name == "show_directions":
        place, address = str(args.get("name", "")).strip(), str(args.get("address", "")).strip()
        if not place: return {"ok": False, "error": "no place name"}
        # The Maps place ID makes the phone open exactly that place, not a namesake.
        key = place.lower()
        hit = next((p for p in maps_places if p.get("title") and (p["title"].lower() in key or key in p["title"].lower())), {})
        pid = (hit.get("placeId") or "").removeprefix("places/")
        publish_destination(place, address, by="gemini", place_id=pid)
        status("Directions", f"Tap Go on your phone: {place}", "ok", 30000)
        return {"ok": True, "shown_on_phone": place, "note": "The driver taps Go on the phone to open Google Maps. Tell them that."}
    if name == "set_light":
        client.publish("car/atech/light", json.dumps({"on": bool(args.get("on"))}))
        return {"ok": True, "light_on": bool(args.get("on"))}
    if name == "cabin_status":
        if not last_verdict: return {"ok": False, "error": "the camera monitor has no verdict yet"}
        v = dict(last_verdict); v["seconds_ago"] = round(time.time() - v.pop("captured_at", time.time()))
        return {"ok": True, **v}
    return {"ok": False, "error": f"no function {name}"}

def ask_google(s, query):
    if not GEMINI_KEY:
        return say(s, "Google isn't set up yet. Add a Gemini API key to the hub's secrets.", "No Google key",
                   "Add gemini_api_key to jetson-hub/secrets.json", "alert")
    status("Asking Google", query, "fg", 45000)
    say(s, "One moment.", "Asking Google", query, "fg")      # a tool chain can take a while: no silent wait
    loc = phone_location()
    where = (" Google Maps already knows the car's position. Use Maps only when the driver asks about places, distances or"
             " routes: one query like 'pharmacy near me' is enough. Don't use Maps just to find out where the car is, and never"
             " search the web for coordinates." if loc else " The car's position is unknown.")
    system = ("You are the co-pilot voice assistant in a car, talking to the driver. Use Google Search for current facts and "
              "Google Maps for places, opening hours, distances and routes. When the driver wants to go somewhere, find the place with "
              "Google Maps, then call show_directions with its name and address. Use the car's functions when the driver asks for an "
              f"action (contacts: {', '.join(CONTACTS) or 'mom, dad'}). Never invent results: if a tool failed, say so. "
              "The request comes from speech recognition of a driver who isn't a native speaker of the local language: place names may be "
              "spelled the way they sound in English (\"campy\" for Kamppi); look for the real place nearby that sounds like it. "
              "Your final answer is read aloud while driving: at most three short sentences, plain words, no lists, markdown, links or emoji. "
              f"Now (UTC): {time.strftime('%A %d %B %Y, %H:%M', time.gmtime())}.{where}")
    _, heard, name = fix_place(query, in_sentence=True)
    if name: log(f"place name for Gemini: heard {heard!r}, maybe {name!r}")
    note = (f"\n(Speech recognition note: \"{heard}\" is probably the local place name \"{name}\".)" if name else "")
    contents = [{"role": "user", "parts": [{"text": query + note}]}]
    maps_places.clear()
    t = time.time(); answer = ""
    try:
        for step in range(5):
            res = gemini_call(contents, system, loc)
            cand = (res.get("candidates") or [{}])[0]
            content = cand.get("content") or {}
            for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
                if ch.get("maps"): maps_places.append(ch["maps"])
            parts = content.get("parts") or []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            for p in parts:
                if "toolCall" in p: log("Gemini used", p["toolCall"].get("toolType"), p["toolCall"].get("args"))
            if not calls:
                answer = " ".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")).strip()
                break
            contents.append(content)                       # as returned: keeps tool calls and thought signatures
            replies = []
            for c in calls:
                out = run_function(c.get("name"), c.get("args") or {}, query)
                log("Gemini called", c.get("name"), c.get("args"), "->", out)
                replies.append({"functionResponse": {"name": c.get("name"), "id": c.get("id"), "response": out}})
            contents.append({"role": "user", "parts": replies})
            if not current(s): return
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log("Gemini error", e.code, detail)
        quota = e.code == 429 or "quota" in detail.lower() or "billing" in detail.lower()
        return say(s, "Google is out of credit right now." if quota else "Google didn't answer. " + ("The API key was refused." if e.code in (401, 403) else ""),
                   "Google error", f"HTTP {e.code}" + (": quota or credits" if quota else ""), "alert")
    except OSError as e:
        log("Gemini unreachable:", e)
        return say(s, "I can't reach Google right now. Is the car online?", "Offline", str(e)[:100], "alert")
    answer = re.sub(r"[*_#`]|\[\d+\]", "", answer)
    log(f"Gemini answered in {time.time() - t:.1f}s: {answer!r}")
    if not answer: return say(s, "Google had no answer for that.", "No answer", query, "warn")
    say(s, answer, "Google", answer, "fg")

# ---------- text to speech, streamed to the board's speaker ----------
def say(s, text, title, body, color):
    if not current(s): return
    log("saying:", text)
    status(title, body, color, 15000)
    if piper is None: return
    import soxr
    q = queue.Queue(maxsize=8)
    def synth():
        try:
            for chunk in piper.synthesize(text):
                if not current(s): break
                q.put(soxr.resample(chunk.audio_int16_array, chunk.sample_rate, SR).astype("<i2").tobytes())
        except Exception as e: log("text-to-speech failed:", e)
        finally: q.put(None)
    threading.Thread(target=synth, daemon=True).start()
    start, sent = None, 0.0
    while (pcm := q.get()) is not None:
        # If the next sentence took longer to make than the board had buffered, the speaker
        # ran dry: restart the clock from now so the lead is rebuilt instead of overflowing it.
        start = max(start, time.monotonic() - sent) if start else time.monotonic()
        for i in range(0, len(pcm), PCM_CHUNK):
            if not current(s): return                         # a new press interrupts the answer
            ahead = sent - (time.monotonic() - start)
            if ahead > PLAY_LEAD_S: time.sleep(ahead - PLAY_LEAD_S)
            part = pcm[i:i + PCM_CHUNK]
            client.publish("car/atech/voice/pcm", part)
            sent += len(part) / 2 / SR

# ---------- Nemotron: watch the cabin for danger near the driver ----------
# One request in flight at a time: fetch the newest frame from vision.py only when the previous
# answer is back (the shared endpoint can take a minute per call). A "danger" verdict goes to the
# hub on car/vision/threat, which alerts the contacts silently like SOS (no sound or screen in the
# car: an attacker mustn't learn that help was called). Every verdict is also published, retained,
# on car/vision/nemotron, so the demo can show what Nemotron sees.
WATCH_PROMPT = """You are the safety monitor of a car. The images are frames from a camera inside the car (their order is given below).
Answer two separate questions.

1. "level": is anyone near the driver attacking the driver or taking control of the car?
"danger" = clearly happening now: a weapon (knife, gun, bottle raised to strike); hitting, choking or physically restraining the driver; grabbing the steering wheel, gear lever or handbrake. This silently alerts the driver's family, so use it only for these.
"watch" = risky but not an attack: something blocking the driver's view or covering their eyes or face, objects thrown, a scuffle starting, a hand moving toward the wheel.
"none" = everything else: talking, eating, phones, normal gestures, sleeping, empty seats, a blurry or dark frame.
"what" = one short factual sentence of what you see (empty if none).

2. "instruction": is someone in the BACK seat making one of these hand signs to the driver? Ignore the driver and the front passenger.
- volume_down: flat hand, palm down, pressed downward
- volume_up: flat hand, palm up, lifted upward
- slower: flat hand, palm facing the front seats, held still like a stop sign
- faster: hand rolling forward in small circles
- thank_you: fingertips of a flat hand touching the chin, or moving forward and down from it
- need_break: both hands forming a T (time-out)
- all_good: thumbs up
Use "none" unless the sign is clear.

Answer with JSON only: {"level": "none"|"watch"|"danger", "what": "...", "people": <number of people visible>, "instruction": "<one id above or none>"}"""
GESTURES = {"volume_down": "Could you turn the volume down, please?", "volume_up": "Could you turn the volume up, please?",
            "slower": "Could you slow down, please?", "faster": "Could you go a bit faster?", "thank_you": "Thank you!",
            "need_break": "Can we stop for a break soon?", "all_good": "All good back here."}
WATCH_SCHEMA = {"type": "object", "properties": {"level": {"type": "string", "enum": ["none", "watch", "danger"]},
                "what": {"type": "string"}, "people": {"type": "integer"},
                "instruction": {"type": "string", "enum": ["none", *GESTURES]}}, "required": ["level", "what", "people", "instruction"]}
MAX_FRAME_AGE_S = 10
HUB_LEVEL = {"none": "low", "watch": "medium", "danger": "high"}   # hub.py alerts on "high" only

def speak_aside(text, title):
    # Say something that isn't an answer to a press (a back-seat sign), unless the driver is
    # talking to the car right now. It becomes the current "session", so a press interrupts it.
    global session
    with lock:
        if session and session["listening"]: return False
        s = session = {"id": "aside", "sid": None, "chunks": {}, "listening": False, "started": time.time()}
    say(s, text, title, text, "fg")
    return True

def nemotron(content, max_tokens=300, schema=None):
    body = {"model": NEMOTRON_MODEL, "max_tokens": max_tokens, "temperature": 0, "messages": [{"role": "user", "content": content}],
            "chat_template_kwargs": {"enable_thinking": False}}      # a verdict, not an essay: answer directly
    if schema: body["response_format"] = {"type": "json_schema", "json_schema": {"name": "verdict", "schema": schema}}
    req = urllib.request.Request(NEMOTRON_URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + VERDA_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r: res = json.load(r)
    return res["choices"][0]["message"].get("content") or ""

WATCH_FRAMES, WATCH_SPAN_S, WATCH_WIDTH = 3, 1.5, 640    # a short burst shows movement (a hand reaching for the wheel)

def shrink(jpg):
    # 1280x720 -> 640 px wide, ~30 KB: a quarter of the upload over the hotspot and fewer image tokens.
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(jpg)); im.thumbnail((WATCH_WIDTH, WATCH_WIDTH))
    out = io.BytesIO(); im.convert("RGB").save(out, "JPEG", quality=75); return out.getvalue()

def camera_frames():
    # vision.py's newest frames, oldest first. Falls back to the single newest photo.
    import base64
    try:
        with urllib.request.urlopen(f"{VISION_URL}/frames?n={WATCH_FRAMES}&span={WATCH_SPAN_S}", timeout=5) as r:
            fr = json.load(r)["frames"]
        if fr: return [shrink(base64.b64decode(f["jpeg_b64"])) for f in fr], float(fr[-1]["captured_at"])
    except urllib.error.HTTPError as e:
        if e.code == 503: raise
    with urllib.request.urlopen(VISION_URL + "/photo.jpg", timeout=5) as r:
        return [shrink(r.read())], float(r.headers.get("X-Captured-At") or time.time())

# Camera state for the board's screen (a crossed-out webcam icon when not "ok"), retained:
# "ok", "dark" (the picture is almost uniformly black: lid closed, lens blocked, or a dark cabin at night;
# the camera has no infrared, so Nemotron couldn't see anything either) or
# "off" (no frames: camera unplugged or the Uno Q down). Nemotron isn't asked while it's not "ok".
DARK_MEAN, DARK_SPREAD = 20, 12          # 0-255 brightness: a closed lid gives about 0-5, a dark car at night more texture
cam_state = None

def covered(jpg):
    import io
    from PIL import Image, ImageStat
    st = ImageStat.Stat(Image.open(io.BytesIO(jpg)).convert("L"))
    return st.mean[0] < DARK_MEAN and st.stddev[0] < DARK_SPREAD

def camera_state(state, why=""):
    global cam_state
    if state == cam_state: return
    cam_state = state
    log(f"camera {state}" + (f": {why}" if why else "") + ("" if state == "ok" else ", Nemotron paused"))
    client.publish("car/vision/camera", json.dumps({"state": state}), qos=1, retain=True)

def watch_loop():
    import base64
    if not VERDA_KEY:
        log("Nemotron off: no VERDA_INFERENCE_KEY"); return
    prev_gesture, spoken_gesture = "none", None
    while True:
        try:
            try: frames, captured = camera_frames()
            except (urllib.error.HTTPError, OSError) as e:
                camera_state("off", e); time.sleep(5); continue
            if time.time() - captured > MAX_FRAME_AGE_S: camera_state("off", "frames too old"); time.sleep(2); continue
            if covered(frames[-1]): camera_state("dark"); time.sleep(2); continue   # no point asking about a black frame
            camera_state("ok")
            t = time.time()
            intro = (f"{len(frames)} frames from the last {WATCH_SPAN_S} s, oldest first, newest last. Judge what is happening now"
                     " (the newest frame), using the earlier ones to see movement." if len(frames) > 1 else "One frame.")
            text = nemotron([{"type": "text", "text": WATCH_PROMPT + "\n\n" + intro},
                             *({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode()}} for f in frames)],
                            schema=WATCH_SCHEMA)
            m = re.search(r"\{.*\}", text, re.S)
            v = json.loads(m.group(0)) if m else {"level": "none", "what": "", "people": 0}
            level = v.get("level") if v.get("level") in ("none", "watch", "danger") else "none"
            took = time.time() - t
            gesture = v.get("instruction") if v.get("instruction") in GESTURES else "none"
            log(f"Nemotron ({took:.0f}s, frame {t - captured:.1f}s old): {level} {v.get('what', '')!r} people={v.get('people')} sign={gesture}")
            verdict = {"level": HUB_LEVEL[level], "what": str(v.get("what", ""))[:200], "people": v.get("people"),
                       "instruction": gesture, "captured_at": captured, "took_s": round(took, 1), "model": NEMOTRON_MODEL}
            client.publish("car/vision/nemotron", json.dumps(verdict), qos=0, retain=True)
            last_verdict.clear(); last_verdict.update(verdict)
            if level == "danger":
                client.publish("car/vision/threat", json.dumps(verdict), qos=1)   # hub alerts silently
            # Back-seat sign: spoken after two identical replies in a row, then not again until it changes.
            if gesture != "none" and gesture == prev_gesture and gesture != spoken_gesture:
                if speak_aside("From the back seat: " + GESTURES[gesture], "Back seat"): spoken_gesture = gesture
            if gesture == "none": spoken_gesture = None
            prev_gesture = gesture
        except Exception as e:
            log("Nemotron watch error, retrying in 15 s:", repr(e)); time.sleep(15)

client.on_connect = on_connect
client.on_message = on_message
while True:
    try: client.connect(MQTT_HOST, 1883, 30); break
    except Exception as e: log("waiting for broker:", e); time.sleep(2)
client.loop_start()
threading.Thread(target=udp_loop, daemon=True).start()
threading.Thread(target=prepare_llm, daemon=True).start()
threading.Thread(target=watch_loop, daemon=True).start()
threading.Thread(target=places_loop, daemon=True).start()
load_models()
models_loaded.set()
log("voice ready" + ("" if GEMINI_KEY else " (no Gemini key: 'ask Google' is off)"))
while True: time.sleep(3600)
