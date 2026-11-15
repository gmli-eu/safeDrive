# SafeDrive voice assistant (runs on the Jetson, next to hub.py).
# Press the port-5 button and talk: "hey car, send a message to mom, I'm running 5 min late",
# "hey car, ask Google what's the weather today", "hey car, turn on the light".
# The back seat has its own board (button on port 9, the passenger intercom): the same assistant,
# but questions only. It can never message the family, switch the light or send directions.
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

import collections, json, pathlib, queue, re, socket, threading, time, urllib.error, urllib.request, uuid
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
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")              # the driver: English only, the most accurate for it
WHISPER_MULTI_MODEL = os.environ.get("WHISPER_MULTI_MODEL", "small")      # the back seat: any language, detected
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
# Local voices for the back seat's language (Whisper's language code -> Piper voice). Portuguese is
# Portugal's. Downloaded on first use (about 63 MB each); PREFETCH_LANGS are fetched at start.
PIPER_VOICES = {"pt": "pt_PT-tugão-medium", "es": "es_ES-davefx-medium", "fr": "fr_FR-siwis-medium", "de": "de_DE-thorsten-medium",
                "it": "it_IT-paola-medium", "fi": "fi_FI-harri-medium", "sv": "sv_SE-nst-medium", "nl": "nl_NL-mls-medium",
                "no": "no_NO-talesyntese-medium", "da": "da_DK-talesyntese-medium", "pl": "pl_PL-gosia-medium", "cs": "cs_CZ-jirka-medium",
                "ru": "ru_RU-irina-medium", "uk": "uk_UA-ukrainian_tts-medium", "ro": "ro_RO-mihai-medium", "el": "el_GR-rapunzelina-medium",
                "hu": "hu_HU-anna-medium", "tr": "tr_TR-dfki-medium", "ca": "ca_ES-upc_ona-medium", "et": "et_EE-news-medium",
                "zh": "zh_CN-huayan-medium", "ar": "ar_JO-kareem-medium", "vi": "vi_VN-vais1000-medium", "hi": "hi_IN-pratham-medium"}
PIPER_VOICES.update(json.loads(os.environ.get("PIPER_VOICES") or "{}"))    # override or add: {"pt": "pt_BR-faber-medium"}
PREFETCH_LANGS = os.environ.get("PREFETCH_LANGS", "pt").split(",")
LANG_NAMES = {"en": "English", "pt": "Portuguese", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "fi": "Finnish",
              "sv": "Swedish", "nl": "Dutch", "no": "Norwegian", "da": "Danish", "pl": "Polish", "cs": "Czech", "ru": "Russian",
              "uk": "Ukrainian", "ro": "Romanian", "el": "Greek", "hu": "Hungarian", "tr": "Turkish", "ca": "Catalan", "et": "Estonian",
              "zh": "Chinese", "ar": "Arabic", "vi": "Vietnamese", "hi": "Hindi", "ja": "Japanese", "ko": "Korean"}
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Translator mode: Gemini hears the recording and translates it in one call; Gemini TTS speaks
# the back seat's language (Piper only speaks English).
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "gemini-3.8-flash")
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-3.8-flash-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "Kore")
# Nemotron 3.5 Super VL (Verda endpoint) watches the in-car camera for danger near the driver.
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "https://containers.datacrunch.io/nemotron-3-5-super-vl-b300/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nemotron-3.5-super-vl")
VERDA_KEY = os.environ.get("VERDA_INFERENCE_KEY", "")
VISION_URL = os.environ.get("VISION_URL", "http://127.0.0.1:8091")   # vision.py: newest camera frame
CONTACTS = json.loads(os.environ.get("CONTACTS") or "[]")      # Telegram bot names, e.g. ["mom", "dad"]
HUB_STATE = pathlib.Path(os.environ.get("HUB_STATE", "/hub/telegram.json"))   # hub.py's state: last phone fix
DATA = pathlib.Path(os.environ.get("DATA_DIR", "/data"))

SR = 16000                 # board mic and speaker sample rate
# How far ahead of the speaker the PCM is sent. The board buffers 1.5 s and drops what doesn't fit;
# 0.6 s left gaps whenever the hotspot stalled (it also carries the camera's ~1.3 MB/s). 1.0 s rides
# out longer stalls and still leaves room for a burst of late packets. Speech starts just as soon.
PLAY_LEAD_S = 1.0
PCM_CHUNK = 3200           # bytes per MQTT message: 100 ms
MIC_PORT = 1885            # the board sends mic audio here over UDP (see udp_loop)

def log(*a): print(time.strftime("%H:%M:%S"), "[voice]", *a, flush=True)

# ---------- models ----------
whisper = whisper_multi = piper = vad = None
whisper_lock = threading.Lock()
voices = {}                # Whisper language code -> loaded PiperVoice ("en" is `piper`)
voices_lock = threading.Lock()
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
    piper = load_voice(PIPER_VOICE)
    voices["en"] = piper
    log("text-to-speech ready", PIPER_VOICE)

def load_voice(voice):
    # A Piper voice from Hugging Face (downloaded once into the data volume).
    import urllib.parse
    from piper import PiperVoice
    lang, region, name, quality = re.match(r"(\w+)_(\w+)-(\w+)-(\w+)", voice).groups()
    vdir = DATA / "piper"; vdir.mkdir(parents=True, exist_ok=True)
    base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/" + urllib.parse.quote(f"{lang}/{lang}_{region}/{name}/{quality}/{voice}")
    for ext in (".onnx", ".onnx.json"):
        f = vdir / (voice + ext)
        if not f.exists():
            log("downloading voice", f.name)
            urllib.request.urlretrieve(base + ext, str(f) + ".part"); os.rename(str(f) + ".part", f)
    return PiperVoice.load(vdir / (voice + ".onnx"))

# Local voices by regional variety (BCP-47). Used first wherever the back seat's variety is known:
# a real Portugal voice says Portugal Portuguese, where Gemini's voice drifts to Brazilian.
VARIETY_VOICES = {"pt-PT": "pt_PT-tugão-medium", "pt-BR": "pt_BR-faber-medium", "es-ES": "es_ES-davefx-medium",
                  "es-MX": "es_MX-claude-high", "es-AR": "es_AR-daniela-high", "nl-BE": "nl_BE-nathalie-medium", "en-GB": "en_GB-alba-medium"}
voices_by_name = {}

def voice_for_variety(code):
    # The local voice for exactly that variety ("pt-PT"), or None: never another country's voice.
    base, _, region = code.partition("-")
    name = VARIETY_VOICES.get(f"{base.lower()}-{region.upper()}")
    if not name and not region and base.lower() in PIPER_VOICES: return voice_for(base.lower())   # no region known: the default
    if not name and region and PIPER_VOICES.get(base.lower(), "").startswith(f"{base.lower()}_{region.upper()}-"): return voice_for(base.lower())
    if not name: return None
    if PIPER_VOICES.get(base.lower()) == name: return voice_for(base.lower())       # already loaded as the language's default
    with voices_lock:
        if name not in voices_by_name:
            try: voices_by_name[name] = load_voice(name); log(f"voice ready for {code}: {name}")
            except Exception as e: log(f"voice for {code} failed: {e!r}"); return None
    return voices_by_name[name]

def voice_for(lang):
    # The local voice for a language, loaded (and downloaded) on first use; None if there's none.
    if lang in voices: return voices[lang]
    if lang not in PIPER_VOICES: return None
    with voices_lock:
        if lang not in voices:
            try:
                voices[lang] = load_voice(PIPER_VOICES[lang]); log(f"voice ready for {lang}: {PIPER_VOICES[lang]}")
            except Exception as e:
                log(f"voice for {lang} failed: {e!r}"); return None
    return voices[lang]

def load_multilingual():
    # The back seat's Whisper (any language): loaded after the driver's, in the background. Until it's
    # ready the back seat uses the English model, as before. Then the prefetched voices.
    global whisper_multi
    from faster_whisper import WhisperModel
    try:
        t = time.time()
        whisper_multi = WhisperModel(WHISPER_MULTI_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        log(f"multilingual speech-to-text ready ({WHISPER_MULTI_MODEL}, {time.time() - t:.0f}s)")
    except Exception as e:
        log("multilingual speech-to-text failed, the back seat stays English-only:", repr(e))
    for lang in PREFETCH_LANGS:
        if lang.strip(): voice_for(lang.strip())

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
# Two boards, each with its own button, mic and speaker, and its own topics:
#   driver:    car/atech/voice/{start,end,pcm,stop,status}   (port 5; the screen shows /status)
#   passenger: car/passenger/voice/{start,end,pcm,stop}      (port 9 on the back-seat board, no screen)
# Each board has its own current press, so the back seat and the driver can ask at the same time.
BOARD_TOPIC = {"driver": "car/atech/voice", "passenger": "car/passenger/voice"}
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="car-voice")
lock = threading.Lock()
sessions = {}              # board -> its current press: dict(id, board, sid, chunks, listening, started)
send_results = {}          # message id -> hub's reply
send_done = threading.Condition(lock)
here = threading.local()   # .board: the board the request being handled came from

def status(title, body="", color="fg", ms=8000, board=None):
    # Shown on the driver board's screen. ASCII only, no quotes (the board's parser is simple).
    # The passenger board has no screen, and its requests never show on the driver's.
    if (board or getattr(here, "board", "driver")) != "driver": return
    clean = lambda s: re.sub(r'[^\x20-\x7e]', '', s.replace('"', "'").replace("\\", "/"))
    client.publish("car/atech/voice/status", json.dumps({"title": clean(title)[:13], "body": clean(body)[:200], "color": color, "ms": ms}))

def on_connect(c, userdata, flags, rc, props=None):
    log("MQTT connected", rc)
    c.subscribe([("car/atech/voice/#", 0), ("car/passenger/voice/#", 0), ("car/voice/send/result", 1), ("car/translator", 1),
                 ("car/cabin/climate", 0), ("car/cabin/comfort", 1),
                 ("car/cabin/occupancy", 0), ("car/vision/pause", 0)])

def on_message(c, userdata, m):
    board = next((b for b, t in BOARD_TOPIC.items() if m.topic.startswith(t + "/")), None)
    if board and m.topic.endswith("/pcm"): return          # our own output
    try: data = json.loads(m.payload.decode() or "null")
    except Exception: data = None
    if board and m.topic.endswith("/start"):
        sid = (data or {}).get("sid")
        # The driver board owns translator mode and sends it with every press ("tr"): follow it, in
        # case the hub missed a change (restart, lost message).
        if board == "driver" and isinstance((data or {}).get("tr"), bool) and data["tr"] != translator["on"]:
            log("translator state from the driver's press:", data["tr"])
            set_translator(data["tr"], announce=False)
        with lock:
            s = sessions[board] = {"id": uuid.uuid4().hex[:8], "board": board, "sid": sid, "chunks": early.pop(sid, {}),
                                   "listening": True, "started": time.time(), "translate": translator["on"]}
        log(f"listening ({board}{', translator' if s['translate'] else ''})", s["id"])
        threading.Thread(target=listen, args=(s,), daemon=True).start()
    elif board and m.topic.endswith("/end"):
        reason = (data or {}).get("reason")
        log(f"{board} board stopped listening:", reason)
        with lock:
            s = sessions.get(board)
            if reason == "accident": sessions.pop(board, None)   # cut off mid-sentence: never act on half a request
            elif s: s["listening"] = False
    elif m.topic == "car/translator" and isinstance(data, dict):
        if bool(data.get("on")) != translator["on"]: set_translator(bool(data.get("on")))
    elif m.topic == "car/cabin/occupancy" and isinstance(data, dict):
        cabin_people.clear(); cabin_people.update(data)
    elif m.topic == "car/vision/pause" and isinstance(data, dict):
        # tools/eval_watch.py record: staged scenes (choking, closed eyes) mustn't alert anyone.
        # At most WATCH_PAUSE_MAX_S per message: the recorder renews it, and the watch comes back by itself.
        try: watch_pause["until"] = time.time() + max(0.0, min(float(data.get("for_s") or 0), WATCH_PAUSE_MAX_S))
        except (TypeError, ValueError): pass
    elif m.topic == "car/cabin/climate" and isinstance(data, dict):
        cabin_climate.clear(); cabin_climate.update(data)
    elif m.topic == "car/cabin/comfort" and isinstance(data, dict):
        # Retained: after a restart the old state comes back, and mustn't be said again.
        if data.get("state") in ("hot", "cold") and time.time() - float(data.get("at") or 0) < 60:
            threading.Thread(target=cabin_warning, args=(data,), daemon=True).start()
    elif m.topic == "car/voice/send/result" and isinstance(data, dict):
        with lock:
            send_results[data.get("id")] = data
            send_done.notify_all()

def current(s):
    with lock: return sessions.get(s["board"]) is s

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
            s = next((x for x in sessions.values() if x["sid"] == sid), None)
            if s:
                if seq < 2000: s["chunks"][seq] = pkt[8:]            # 64 s cap; stragglers after "end" still count
            elif seq < 50:
                if sid not in early and len(early) >= 8: early.pop(next(iter(early)))   # forget the oldest
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
    here.board = s["board"]
    while True:
        time.sleep(0.1)
        if not current(s): return
        with lock:
            if not s["listening"]: break
        if time.time() - s["started"] > 25:
            client.publish(BOARD_TOPIC[s["board"]] + "/stop", "{}"); break
    time.sleep(0.2)                                          # the last packets may still be on their way
    with lock:
        if sessions.get(s["board"]) is not s: return
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
    # One transcription at a time: the driver and the back seat may both be talking.
    # Keep the prompt to the wake words: Whisper sometimes repeats its prompt on unclear audio.
    # The contact names as hints, so a short "Dad" isn't heard as "that" or "dead".
    hint = "Hey car." + "".join(f" {c.capitalize()}." for c in CONTACTS)
    with whisper_lock:
        segs, _ = whisper.transcribe(audio, language="en", beam_size=3, vad_filter=False, initial_prompt=hint)
        text = " ".join(x.text.strip() for x in segs).strip()
    log(f"heard in {time.time() - t:.1f}s: {text!r}")
    return fix_names(WAKE.sub("", text).strip())

def transcribe_any(pcm):
    # The back seat: any language. -> (text, Whisper's language code). English-only model until the
    # multilingual one is loaded.
    if whisper_multi is None: return transcribe(pcm), "en"
    audio = to_float(pcm)
    if vad and not vad.get_speech_timestamps(audio, vad.VadOptions(threshold=0.5)):
        log("no speech in the recording")
        return "", "en"
    t = time.time()
    with whisper_lock:
        segs, info = whisper_multi.transcribe(audio, beam_size=3, vad_filter=False)
        text = " ".join(x.text.strip() for x in segs).strip()
    lang = info.language or "en"
    log(f"heard ({lang}, {info.language_probability:.2f}) in {time.time() - t:.1f}s: {text!r}")
    text = WAKE.sub("", text).strip()
    return (fix_names(text) if lang == "en" else text), lang

# ---------- what does the driver want? ----------
ACTIONS = ["send_message", "ask_google", "directions", "light", "climate", "answer", "unclear"]
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
- climate: the temperature or humidity INSIDE the car ("how warm is it in here?", "what's the temperature in the car?"). Not the weather outside: that's ask_google.
- answer: anything else you can answer yourself (simple facts, maths, jokes, small talk). "reply" is the spoken answer: one or two short sentences, no lists or symbols.
- unclear: empty, cut off, or not understandable."""

EXAMPLES = [
    ("send message to mom, I'm running 5 min late", {"action": "send_message", "to": ["mom"], "text": "I'm running 5 min late."}),
    ("tell mom and dad that I'm leaving the gym now", {"action": "send_message", "to": ["all"], "text": "I'm leaving the gym now."}),
    ("ask google what's the weather for today", {"action": "ask_google", "query": "What's the weather for today?"}),
    ("take me to the central railway station", {"action": "directions", "place": "central railway station"}),
    ("turn off the light", {"action": "light", "on": False}),
    ("how hot is it in the car", {"action": "climate"}),
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
    if (re.search(r"\b(?:temperature|degrees|humid\w*|how (?:hot|warm|cold))\b", t, re.I)
            and not re.search(r"\b(?:outside|weather|forecast|today|tomorrow)\b", t, re.I)): return {"action": "climate"}
    if re.search(r"\b(?:weather|forecast|news|traffic|score)\b", t, re.I): return {"action": "ask_google", "query": t}   # the car then asks for "ask Google"
    m = re.search(r"\b(?:directions?|route|navigate|navigation|take me|drive me|get me)\s+(?:to|for)\s+(?:the\s+)?(.+)", t, re.I)
    if m: return {"action": "directions", "place": re.sub(r"[\s,]+please$", "", m.group(1).strip(" .?!"), flags=re.I)}
    m = re.search(r"\blights?\b.*\b(on|off)\b|\b(on|off)\b.*\blights?\b", t, re.I)
    if m: return {"action": "light", "on": (m.group(1) or m.group(2)).lower() == "on"}
    return {"action": "unclear"}

def local_answer(text, lang="en"):
    where = "" if lang == "en" else (f" Answer in {LANG_NAMES.get(lang, lang)}, in the same regional variety as the question"
                                     + (" (European Portuguese, as in Portugal, unless the question is clearly Brazilian)" if lang == "pt" else "") + ".")
    r = ollama("/api/chat", {"model": LLM_MODEL, "stream": False, "keep_alive": -1, **NO_THINK,
                             "options": {"temperature": 0.3, "num_predict": 160},
                             "messages": [{"role": "system", "content": "You are a car's voice assistant. Answer in one or two short spoken sentences. No lists or symbols." + where},
                                          {"role": "user", "content": text}]}, timeout=30)
    return r["message"]["content"].strip()

# ---------- actions ----------
def handle(s, pcm):
    status("Thinking...", "", "fg", 30000)
    if s.get("translate"): return translate_turn(s, pcm)
    if s["board"] == "passenger": return handle_passenger(s, pcm)
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
    elif act == "climate":
        said = climate_sentence()
        say(s, said, "Cabin", said, "fg")
    elif act == "answer":
        reply = (intent.get("reply") or "").strip() or local_answer(text)
        say(s, reply, "Answer", reply, "fg")
    else:
        say(s, "Sorry, I didn't understand. You can say: send a message to mom, or ask Google.", "Sorry", "Didn't understand: " + text, "warn")

# ---------- the back seat: questions only ----------
# The passenger board can ask anything (the local model, or Google when they say "Google"), but it
# can never message the driver's family, switch the light or put directions on the driver's phone:
# those are refused here whatever the model decided, and Gemini gets none of those functions.
PASSENGER_ONLY_ASK = "From the back seat I can only answer questions. Ask the driver for that."

# The back seat may speak any language: Whisper names it, and the answer comes back in it. Without
# "Google" everything stays in the car (Gemma answers, a local Piper voice reads it); with "Google"
# Gemini answers in their language and its own voice reads it.
def handle_passenger(s, pcm):
    text, lang = transcribe_any(pcm)
    if not current(s): return
    if lang not in voices and voice_for(lang) is None: lang_out = "en"     # no local voice for it: English
    else: lang_out = lang
    if not text: return say_in(s, lang_out, "Sorry, I didn't catch that.")
    log(f"back seat asked ({lang}):", text)
    if NAMES_GOOGLE.search(text): return ask_google(s, text, lang=lang)
    intent = understand(text)
    if not current(s): return
    act = intent.get("action")
    if act in ("send_message", "light", "directions"):
        log(f"back seat: {act} refused")
        return say_in(s, lang_out, PASSENGER_ONLY_ASK)
    if act == "ask_google":
        return say_in(s, lang_out, "I can't check that offline. Say 'ask Google' to look it up.")
    if act == "climate": return say_in(s, lang_out, climate_sentence())
    if act == "answer":
        reply = ((intent.get("reply") or "").strip() if lang_out == "en" else "") or local_answer(text, lang_out)
        return say(s, reply, "", "", "fg", lang=lang_out)
    say_in(s, lang_out, "Sorry, I didn't understand. Press the button and ask your question again.")

def say_in(s, lang, english):
    # A fixed reply in the back seat's language, translated by the local model (English if it can't).
    text = english
    if lang != "en" and llm_ready:
        try:
            r = ollama("/api/chat", {"model": LLM_MODEL, "stream": False, "keep_alive": -1, **NO_THINK,
                                     "options": {"temperature": 0, "num_predict": 120},
                                     "messages": [{"role": "system", "content": f"Translate the user's text into {LANG_NAMES.get(lang, lang)}"
                                                   " (for Portuguese: as spoken in Portugal). Keep the word Google. Reply with the translation only."},
                                                  {"role": "user", "content": english}]}, timeout=20)
            text = r["message"]["content"].strip() or english
        except Exception as e: log("translating a reply failed:", e)
    say(s, text, "", "", "warn", lang=lang if text != english else "en")

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

# ---------- cabin climate: Modulino Thermo on the Uno Q, read by hub.py ----------
cabin_climate = {}     # newest car/cabin/climate: {"celsius", "humidity", "at"}
cabin_people = {}      # newest car/cabin/occupancy (Uno Q, on-device): {"people", "empty_for_s", "confidence", "at"}

def climate_fresh():
    return cabin_climate.get("celsius") is not None and time.time() - float(cabin_climate.get("at") or 0) < 120

def climate_sentence():
    if not climate_fresh(): return "I can't read the cabin temperature right now. The sensor isn't answering."
    c, h = cabin_climate["celsius"], cabin_climate["humidity"]
    feel = ("Dangerously hot." if c >= 35 else "Quite warm." if c >= 26 else "Comfortable." if c >= 18
            else "A bit cool." if c >= 10 else "Cold.")
    return f"It's {c:.0f} degrees in the car, {h:.0f} percent humidity. {feel}"

CABIN_RETRY_S = 240    # how long a warning waits for the car to be free: less than the hub's 5 min reminder
cabin_pending = {"n": 0}   # only the newest warning waits; an older one still waiting gives way

def car_busy():
    # The driver is talking to the car, an answer is playing, or a request is still being worked
    # on (a slow "ask Google" can take ~25 s before it speaks): a warning now would cut it off.
    with lock:
        d = sessions.get("driver")
        return bool((d and d["listening"]) or speaking["n"] or time.monotonic() < speaking["until"]
                    or (d and d["id"] not in ("aside", "relay") and time.time() - d["started"] < 30))

def cabin_warning(d):
    # hub.py: the cabin has been too hot or too cold for a while (again every 5 min while it
    # lasts). Said calmly, like a hand sign: no alarm sound, only the usual status line.
    # If the car is busy, it waits until it's free (up to CABIN_RETRY_S), instead of being dropped.
    c, hot = d.get("celsius"), d.get("state") == "hot"
    fix = ("Please turn on the " + ("air conditioning" if hot else "heating")
           + ", especially with children, older passengers or pets on board.")
    text = (f"Reminder: it's still {c:.0f} degrees in the car. " if d.get("reminder")
            else f"It's getting {'hot' if hot else 'cold'} in here: {c:.0f} degrees. ") + fix
    with lock: cabin_pending["n"] += 1; me = cabin_pending["n"]
    deadline = time.time() + CABIN_RETRY_S
    if car_busy(): log("cabin comfort: car busy, waiting to say it")
    while time.time() < deadline:
        if cabin_pending["n"] != me: return          # a newer warning took over
        if not car_busy() and speak_aside(text, "Too hot" if hot else "Too cold"):
            log("cabin comfort:", text); return
        time.sleep(1)
    log("cabin comfort: car busy for", CABIN_RETRY_S, "s, warning dropped (the banner still shows)")

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
    {"name": "cabin_status", "description": "What the in-car camera monitor (NVIDIA Nemotron) saw most recently: danger level, description, number of people, the driver's neck (hands on the throat), eyes (open, closed, covered, looking down) and phone use, back-seat hand sign; plus the number of people counted right now by the on-device person detector on the car's Arduino UNO Q.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "cabin_climate", "description": "The temperature (°C) and relative humidity (%) inside the car right now, from the car's own sensor (Arduino Modulino Thermo). Not the weather outside.",
     "parameters": {"type": "object", "properties": {}}},
]
last_verdict = {}      # newest Nemotron verdict (watch_loop)
maps_places = []       # Google Maps places Gemini found in this request: [{"title", "placeId", "uri"}]

PASSENGER_FUNCTIONS = {"cabin_status", "cabin_climate"}   # the back seat may only ask; these functions only read

def gemini_call(contents, system, loc, functions):
    tool_config = {"includeServerSideToolInvocations": True}
    if loc: tool_config["retrievalConfig"] = {"latLng": {"latitude": loc["lat"], "longitude": loc["lon"]}}
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "toolConfig": tool_config,
            "tools": [{"google_search": {}}, {"google_maps": {}}, {"function_declarations": functions}]}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=25) as r: return json.load(r)   # each step; a failure mustn't hang for long

def run_function(name, args, said="", board="driver"):
    if board != "driver" and name not in PASSENGER_FUNCTIONS:
        return {"ok": False, "error": "not allowed from the back seat: passengers can only ask questions"}
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
    if name == "cabin_climate":
        if not climate_fresh(): return {"ok": False, "error": "the cabin temperature sensor has no recent reading"}
        return {"ok": True, "celsius": cabin_climate["celsius"], "humidity_percent": cabin_climate["humidity"]}
    if name == "cabin_status":
        v = dict(last_verdict)
        if v: v["seconds_ago"] = round(time.time() - v.pop("captured_at", time.time()))
        if cabin_people.get("people") is not None and time.time() - float(cabin_people.get("at") or 0) < 10:
            v["people_now_on_device"] = cabin_people["people"]
        if not v: return {"ok": False, "error": "the camera monitor has no verdict yet"}
        return {"ok": True, **v}
    return {"ok": False, "error": f"no function {name}"}

ONE_MOMENT = {"pt": "Um momento.", "es": "Un momento.", "fr": "Un instant.", "de": "Einen Moment.", "it": "Un momento.",
              "fi": "Hetki.", "sv": "Ett ögonblick.", "nl": "Een moment.", "pl": "Chwileczkę.", "da": "Et øjeblik.", "no": "Et øyeblikk."}

def ask_google(s, query, lang="en"):
    if not GEMINI_KEY:
        return say(s, "Google isn't set up yet. Add a Gemini API key to the hub's secrets.", "No Google key",
                   "Add gemini_api_key to jetson-hub/secrets.json", "alert")
    status("Asking Google", query, "fg", 45000)
    if lang == "en": say(s, "One moment.", "Asking Google", query, "fg")      # a tool chain can take a while: no silent wait
    elif lang in ONE_MOMENT and voice_for(lang): say(s, ONE_MOMENT[lang], "", "", "fg", lang=lang)
    loc = phone_location()
    passenger = s["board"] == "passenger"
    functions = [f for f in GEMINI_FUNCTIONS if not passenger or f["name"] in PASSENGER_FUNCTIONS]
    where = (" Google Maps already knows the car's position. Use Maps only when the driver asks about places, distances or"
             " routes: one query like 'pharmacy near me' is enough. Don't use Maps just to find out where the car is, and never"
             " search the web for coordinates." if loc else " The car's position is unknown.")
    who = ("You are the voice assistant in a car, talking to a passenger in the back seat. Use Google Search for current facts "
           "and Google Maps for places, opening hours and distances. The passenger can only ask questions: you cannot send "
           "messages, change anything in the car or give the driver directions for them; if they ask, say they should ask the driver. "
           if passenger else
           "You are the co-pilot voice assistant in a car, talking to the driver. Use Google Search for current facts and "
           "Google Maps for places, opening hours, distances and routes. When the driver wants to go somewhere, find the place with "
           "Google Maps, then call show_directions with its name and address. Use the car's functions when the driver asks for an "
           f"action (contacts: {', '.join(CONTACTS) or 'mom, dad'}). ")
    if passenger and lang != "en":
        who += (f"The passenger speaks {LANG_NAMES.get(lang, lang)}: answer in that language and in the same regional variety they used"
                + (" (European Portuguese, as in Portugal, unless they clearly speak Brazilian Portuguese)" if lang == "pt" else "") + ". ")
    system = (who + "Never invent results: if a tool failed, say so. "
              "The request comes from speech recognition of someone who isn't a native speaker of the local language: place names may be "
              "spelled the way they sound in English (\"campy\" for Kamppi); look for the real place nearby that sounds like it. "
              "Your final answer is read aloud in the moving car, so keep it succinct: answer the question directly in one short sentence,"
              " two only if really needed. No background, extra options, history or caveats unless asked. Plain words, no lists, markdown, links or emoji. "
              f"Now (UTC): {time.strftime('%A %d %B %Y, %H:%M', time.gmtime())}.{where}")
    _, heard, name = fix_place(query, in_sentence=True)
    if name: log(f"place name for Gemini: heard {heard!r}, maybe {name!r}")
    note = (f"\n(Speech recognition note: \"{heard}\" is probably the local place name \"{name}\".)" if name else "")
    contents = [{"role": "user", "parts": [{"text": query + note}]}]
    maps_places.clear()
    t = time.time(); answer = ""
    try:
        for step in range(5):
            res = gemini_call(contents, system, loc, functions)
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
                out = run_function(c.get("name"), c.get("args") or {}, query, s["board"])
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
    if lang != "en":
        # Their language, in Gemini's voice (they said "Google": it may go online); the local voice if that fails.
        code = DEFAULT_VARIETY.get(lang, lang)
        v = voice_for_variety(code)
        if v: return say(s, answer, "Google", answer, "fg", voice=v)       # a local voice of their variety: the right accent
        try: pcm = gemini_tts(answer, code if "-" in code else "")
        except Exception as e:
            log("Gemini voice failed, using the local one:", repr(e)); return say(s, answer, "Google", answer, "fg", lang=lang)
        if not current(s): return
        log(f"saying ({s['board']}, Gemini voice):", answer)
        q = queue.Queue(); q.put(pcm); q.put(None)
        return stream(s, q)
    say(s, answer, "Google", answer, "fg")

# ---------- text to speech, streamed to the board's speaker ----------
# Louder speech without touching the board's volume (0.6 in the firmware: 1.0 clips). Each
# sentence is softly compressed (tanh), so quiet syllables get louder, and its peaks are set to
# VOICE_PEAK of full scale. Full scale broke up on the speaker: at volume 0.6 the amplifier
# distorts near the top, so the peaks stay at 0.75. VOICE_DRIVE 0 turns it off.
VOICE_DRIVE = float(os.environ.get("VOICE_DRIVE", "2.0"))
VOICE_PEAK = float(os.environ.get("VOICE_PEAK", "0.75"))
VOICE_MAX_GAIN = 4.0              # never amplify a near-silent sentence more than this

def louder(pcm16):
    if VOICE_DRIVE <= 0 or not len(pcm16): return pcm16
    a = pcm16.astype(np.float32) / 32768
    peak = float(np.abs(a).max())
    if peak < 1e-3: return pcm16
    a *= min(1.0 / peak, VOICE_MAX_GAIN)
    a = np.tanh(VOICE_DRIVE * a) / np.tanh(VOICE_DRIVE)        # peaks at 1.0, quiet parts up to ~2x
    return (a * VOICE_PEAK * 32767).astype(np.int16)

def say(s, text, title, body, color, lang="en", voice=None):
    if not current(s): return
    log(f"saying ({s['board']}{'' if lang == 'en' else ', ' + lang}):", text)
    status(title, body, color, 15000, board=s["board"])
    voice = voice or voices.get(lang) or voice_for(lang) or piper
    if voice is None: return
    import soxr
    q = queue.Queue(maxsize=8)
    def synth():
        try:
            for chunk in voice.synthesize(text):
                if not current(s): break
                pcm = soxr.resample(chunk.audio_int16_array, chunk.sample_rate, SR)
                q.put(louder(np.asarray(pcm, dtype=np.int16)).astype("<i2").tobytes())
        except Exception as e: log("text-to-speech failed:", e)
        finally: q.put(None)
    threading.Thread(target=synth, daemon=True).start()
    stream(s, q)

# While the car speaks, the cabin watch sends no frames (see watch_loop): Nemotron's uploads share
# the 2.4 GHz hotspot with the speaker's audio, and at ~1 request a second they broke the speech up.
speaking = {"n": 0, "until": 0.0}

def stream(s, q):
    with lock: speaking["n"] += 1
    try: stream_pcm(s, q)
    finally:
        with lock:
            speaking["n"] -= 1
            speaking["until"] = time.monotonic() + PLAY_LEAD_S + 0.5   # the board still plays its buffer

def stream_pcm(s, q):
    # Sends 16 kHz PCM from the queue (None ends it) to the speaker of the session's board, just ahead of playback.
    topic = BOARD_TOPIC[s["board"]] + "/pcm"
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
            client.publish(topic, part)
            sent += len(part) / 2 / SR

# ---------- translator: the back seat's language <-> English for the driver ----------
# The driver turns it on in the screen menu (car/translator). Then the back seat speaks their own
# language on their board: Gemini hears the recording, names the language and translates it, and
# the driver hears it in English (Piper, on the driver's speaker). The driver's voice button then
# replies in English: Gemini translates it into the back seat's language and Gemini TTS speaks it
# on the back-seat speaker. While it's on, presses are only translated: no commands, no messages.
# Only in this mode does the recorded speech go to Google.
translator = {"on": False, "lang": "", "code": ""}

def set_translator(on, announce=True):
    translator.update(on=on, lang="", code="")               # each time: the back seat's language is learned again
    client.publish("car/translator/lang", "", qos=1, retain=True)
    log("translator", "on" if on else "off")
    if not on:
        # Cancelled: drop whatever is still being translated or played, on both boards.
        with lock:
            for b, x in list(sessions.items()):
                if x.get("translate") or x["id"] == "relay": sessions.pop(b, None)
        client.publish("car/passenger/voice/done", "{}")          # the back seat's button stops waiting
    if announce:   # on the back-seat speaker; while it's on, their button flashes until they press it (passenger firmware)
        threading.Thread(target=speak_on, args=("passenger", "Translator on." if on else "Translator off."), daemon=True).start()
FROM_BACK_SCHEMA = {"type": "object", "properties": {"heard": {"type": "string"}, "language": {"type": "string"},
                    "code": {"type": "string"}, "english": {"type": "string"}}, "required": ["heard", "language", "code", "english"]}
TO_BACK_SCHEMA = {"type": "object", "properties": {"heard": {"type": "string"}, "translation": {"type": "string"}},
                  "required": ["heard", "translation"]}

def short_lang(lang, code):
    # For the driver's 26-character screen line: "Portuguese (Portugal)" -> "Portuguese (PT)".
    base, _, _ = lang.partition(" (")
    region = code.split("-")[1].upper() if "-" in code else ""
    return (f"{base} ({region})" if region and "(" in lang else lang)[:15]

def gemini_post(model, body, timeout=45):
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)

def to_wav(audio):
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()

def gemini_hear(prompt, wav, schema):
    import base64
    res = gemini_post(TRANSLATE_MODEL, {
        "contents": [{"parts": [{"text": prompt}, {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0,
                             "thinkingConfig": {"thinkingBudget": 0}}})                # a translation, not an essay: answer at once
    parts = ((res.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return json.loads(next((p["text"] for p in reversed(parts) if "text" in p), "{}"))

# Gemini's voice drifts to Brazilian for Portuguese, and it reads any accent direction out loud (it
# takes no system instruction), so it's only used when there's no local voice for the variety, with
# just the language code.
DEFAULT_VARIETY = {"pt": "pt-PT"}    # Whisper names only the language: the back seat is from Portugal

def gemini_tts(text, code=""):
    # Gemini's voice for the back seat's language -> 16 kHz int16 PCM for the board. "code" (BCP-47,
    # with the region: pt-PT, not pt) keeps the accent of the back seat's own variety; if Gemini
    # doesn't take that code, the text alone still decides (it's written in that variety).
    import base64, io, wave, soxr
    speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_VOICE}}}
    body = lambda sc: {"contents": [{"parts": [{"text": text}]}], "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": sc}}
    try: res = gemini_post(TTS_MODEL, body({**speech, "languageCode": code} if code else speech))
    except urllib.error.HTTPError as e:
        if not code or e.code != 400: raise
        log(f"translator: TTS refused language code {code!r}, using the text alone")
        res = gemini_post(TTS_MODEL, body(speech))
    inline = next(p["inlineData"] for p in res["candidates"][0]["content"]["parts"] if "inlineData" in p)
    data = base64.b64decode(inline["data"])
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w: rate, data = w.getframerate(), w.readframes(w.getnframes())
    else:                                        # raw PCM: "audio/L16;codec=pcm;rate=24000"
        m = re.search(r"rate=(\d+)", inline.get("mimeType", "")); rate = int(m.group(1)) if m else 24000
    pcm = soxr.resample(np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2"), rate, SR)
    return louder(np.asarray(pcm, dtype=np.int16)).astype("<i2").tobytes()

def speak_on(board, text, title="", body="", color="fg", pcm=None, voice=None):
    # Speak on another board than the one that asked: the driver hears the back seat, and back.
    # Waits (up to 25 s) while that board is listening, so it never talks over a press.
    t = time.time()
    while time.time() - t < 25:
        with lock:
            busy = sessions.get(board) and sessions[board]["listening"]
            if not busy:
                s = sessions[board] = {"id": "relay", "board": board, "sid": None, "chunks": {}, "listening": False, "started": time.time()}
                break
        time.sleep(0.2)
    else:
        log(f"translator: {board} kept talking, dropped: {text!r}"); return
    if pcm is None: return say(s, text, title, body, color, voice=voice)
    log(f"saying ({board}, Gemini voice):", text)
    status(title, body, color, 15000, board=board)
    q = queue.Queue(); q.put(pcm); q.put(None)
    stream(s, q)

def translate_turn(s, pcm):
    if not GEMINI_KEY:
        return say(s, "The translator needs Google. Add a Gemini API key to the hub's secrets.", "No Google key",
                   "Add gemini_api_key to jetson-hub/secrets.json", "alert")
    audio = to_float(pcm)
    if vad and not vad.get_speech_timestamps(audio, vad.VadOptions(threshold=0.5)):
        log("translator: no speech in the recording")
        return say(s, "Sorry, I didn't catch that.", "Sorry", "No speech heard. Press and try again.", "warn")
    wav, t = to_wav(audio), time.time()
    try:
        if s["board"] == "passenger":
            hint = f" Last time they spoke {translator['lang']}." if translator["lang"] else ""
            r = gemini_hear("This is a recording of a passenger in the back seat of a car, talking to the driver in their own language."
                            + hint + " heard: exactly what they said, in the original language and its usual script. language: that"
                            " language's English name with the regional variety they speak, judged from their accent, words and grammar,"
                            " e.g. 'Portuguese (Portugal)' or 'Portuguese (Brazil)', 'Spanish (Spain)' or 'Spanish (Mexico)', 'French"
                            " (France)'; just the language if you can't tell. code: its BCP-47 code with the region when known (pt-PT,"
                            " pt-BR, es-ES, es-MX, fr-FR). english: a natural, faithful English translation for the driver, in the first"
                            " person as they said it, with nothing added. If there is no clear speech, use empty strings.",
                            wav, FROM_BACK_SCHEMA)
            english, lang = str(r.get("english", "")).strip(), str(r.get("language", "")).strip()
            log(f"translator: back seat ({lang}, {time.time() - t:.1f}s): {r.get('heard')!r} -> {english!r}")
            if not current(s) or not translator["on"]: return            # cancelled meanwhile
            if not english: return say(s, "Sorry, I didn't catch that.", "", "", "warn")
            if lang and lang != translator["lang"]:
                code = str(r.get("code", "")).strip()
                translator.update(lang=lang, code=code)
                client.publish("car/translator/lang", short_lang(lang, code), qos=1, retain=True)
            client.publish("car/passenger/voice/done", "{}")          # passed on: chime, and the back seat's light goes off
            speak_on("driver", english, (lang.split(" (")[0] or "Back seat")[:13], english, "fg")
        else:
            if not translator["lang"]:
                return say(s, "The back seat hasn't spoken yet, so I don't know their language. Ask them to press their button first.",
                           "Translator", "Waiting for the back seat to speak first.", "warn")
            lang, code = translator["lang"], translator["code"]
            status("Translating...", f"To {lang}", "fg", 30000)
            r = gemini_hear(f"This is a recording of the driver of a car replying in English to a passenger in the back seat who speaks {lang}"
                            f" ({code}). heard: exactly what the driver said, leaving out a leading 'hey car'. translation: a natural,"
                            f" faithful translation into exactly that variety, {lang}: its grammar, words, spelling and forms of address,"
                            " as someone from there says it, never another country's variety (for Portugal: 'estamos a chegar', 'autocarro',"
                            " 'casa de banho', 'tu'/'você' as used in Portugal; not Brazilian forms). In its usual script, in the first person"
                            " as the driver said it, with nothing added. If there is no clear speech, use empty strings.", wav, TO_BACK_SCHEMA)
            heard, said = str(r.get("heard", "")).strip(), str(r.get("translation", "")).strip()
            log(f"translator: driver -> {lang} ({time.time() - t:.1f}s): {heard!r} -> {said!r}")
            if not current(s) or not translator["on"]: return            # cancelled meanwhile
            if not said: return say(s, "Sorry, I didn't catch that.", "Sorry", "No speech heard. Press and try again.", "warn")
            status(f"To {lang.split(' (')[0]}"[:13], heard, "ok", 15000)
            if code.lower().startswith("en"): return speak_on("passenger", said)     # English: the local voice is enough
            v = voice_for_variety(code) if code else None        # a local voice of their own variety, if there is one
            if v:
                if translator["on"]: speak_on("passenger", said, voice=v)
                return
            pcm = gemini_tts(said, code if "-" in code else "")
            if translator["on"]: speak_on("passenger", said, pcm=pcm)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log("translator: Gemini error", e.code, detail)
        say(s, "The translator didn't get an answer from Google.", "Google error", f"HTTP {e.code}", "alert")
    except OSError as e:
        log("translator: Gemini unreachable:", e)
        say(s, "I can't reach Google right now. Is the car online?", "Offline", str(e)[:100], "alert")

# ---------- Nemotron: watch the cabin for danger near the driver ----------
# One request in flight at a time: fetch the newest frame from vision.py only when the previous
# answer is back (the shared endpoint can take a minute per call). A threat (car_actions: a "danger"
# level, or hands, an arm or a cord on the throat) goes to the hub on car/vision/threat, which alerts the contacts silently like SOS (no sound or screen in the
# car: an attacker mustn't learn that help was called). Every verdict is also published, retained,
# on car/vision/nemotron, so the demo can show what Nemotron sees.
# Who may give hand signs: "any" (anyone the camera sees, the driver too: at a demo table everyone
# looks like they sit in front) or "back" (only the back seat, for a camera mounted in a real car).
# With "back", Nemotron ignored nearly every sign at the demo table: it saw the person as the driver.
# ---- watch prompt: tools/eval_watch.py runs this block as it is (needs only os) ----
GESTURE_FROM = os.environ.get("GESTURE_FROM", "any")
SIGN_WHO = ("anyone visible (the driver too)" if GESTURE_FROM == "any" else
            "someone in the BACK seat (ignore the driver and the front passenger)")
WATCH_PROMPT = f"""You are the safety monitor of a car. The images are frames from a camera inside the car (their order is given below).
The DRIVER is the person in the driver's seat; if only one person is visible, that person. An attacker sitting behind the driver is hidden by the seat: only their hands, forearm or a cord show, coming from behind the headrest, the shoulders or the side of the seat, and from the front they look like the driver's own hands.
Answer these questions, in this order.

1. "hands": first describe, in one or two short sentences, every visible hand across ALL frames (not only the last frame): one or both hands; where (at the neck or throat, under the chin, at the mouth, over the eyes, at the ear, raised in front of the body); shape (flat, fist, thumb up, single index finger raised, fingers interlaced, two hands in a perpendicular T/cross, two palms pressed vertically together); which way the palm faces (down, up, inward, toward the camera); anything held (phone, knife, pen, cord, belt, scarf, cup) and anything across the throat; and how the hands move from the first frame to the last. "none" only if no hand is visible in any frame.

2. "neck": based on "hands", what is at the driver's neck and throat (below the chin)?
"arm_or_cord" = an arm or forearm, or a cord, belt, scarf, cable or strap, across or around the front of the throat.
"both_hands" = BOTH hands on the front or sides of the neck/throat (gripping, clutching, or fingers interlaced around it), whoever's hands they seem to be: both hands clutching one's own throat is also the universal sign of choking.
"one_hand" = ONE hand gripping, clutching or pressing the front or side of the neck/throat.
"chin_or_face" = hands under or on the chin, at the mouth, on the cheek or face, or touching the collar (a thinking pose, scratching, a thank_you sign).
"none" = nothing at the neck or face.

3. "eyes": the DRIVER's eyes across ALL frames.
"closed" = the eyelids are clearly shut in EVERY frame (a blink lasts less than one frame, so eyes open in any frame are not closed), including a head nodded or slumped forward or sideways with the eyes shut.
"covered" = something is over the driver's eyes in EVERY frame, so they can't see the road: a hand or hands (their own or someone else's from behind), an object, a cloth, a hood, hat or hoodie pulled down over the eyes. Not sunglasses or glasses, not a hand only at the mouth or chin.
"down" = the eyes are open or half open but looking down (at a phone, the lap or a screen below the camera).
"open" = open and uncovered in at least one frame.
"not_visible" = no driver, the face is turned away or too small, or sunglasses.

4. "phone": is the DRIVER using a phone, across the frames? A passenger's phone doesn't count.
"using" = the driver holds a phone and looks at it, types or scrolls on it, or holds it to the ear (a call).
"holding" = a phone in the driver's hand, but they look away from it (e.g. at the road).
"none" = no phone in the driver's hand: a phone in a holder on the dashboard or windscreen, or lying on a seat, is "none".

5. "level": what is happening to the driver right now? ONLY the driver may be visible: judge by what is happening to the driver, whether or not another person can be seen, and even if the hands or weapon appear to belong to the driver.
"danger" = clearly happening now: "neck" is arm_or_cord or both_hands; "neck" is one_hand and that arm comes from behind or beside the seat, or the driver shows distress (head thrown back, mouth wide open, face strained); a weapon (knife, gun, bottle raised to strike); hitting or physically restraining the driver (pulling them back by the head, hair or shoulders); grabbing or yanking the steering wheel, gear lever or handbrake. This silently alerts the driver's family, so use it only for these.
"watch" = risky but not an attack: "neck" is one_hand with the driver's own arm and no distress; something blocking the driver's view, objects thrown, a scuffle starting, a hand reaching for the wheel.
"none" = everything else ("neck" chin_or_face or none): talking, eating, phones, normal gestures, hand signs, sleeping, empty seats, a blurry or dark frame.

6. "what" = one short factual sentence of what you see (who is where, what they are doing), also when nothing is wrong.

7. "people" = the number of people visible.

8. "instruction": based on "hands", is {SIGN_WHO} deliberately making one of these hand signs across the frames? People may sit close to the camera and their hands may be partly cut off.
Check in this order:
a) Are BOTH hands raised together?
   - If one hand is horizontal and the other upright/vertical touching it (sports time-out T), or the two hands/forearms are crossed in an X, answer need_break.
   - If both flat hands are pressed vertically palm-to-palm with fingers pointing up (prayer / thank-you pose), answer thank_you.
   - volume_down, volume_up, slower, faster and all_good are never two hands.
b) Otherwise, if ONE hand makes one of the signs below (even if the motion finishes and the hand lowers by the last frame), answer that sign:
- thank_you: ONE flat hand with fingers together touching or held close to the chin, mouth or lips (palm facing inward/person) in any frame, or moving forward and downward away from the chin/mouth. This counts even though the hand is near the face.
- faster: ONE hand with ONLY the index finger pointing up (other fingers curled), whether circling, moving slightly, or held up steady (a small finger circle looks nearly still across 3 snapshots), OR a hand rolling in circles.
- all_good: ONE hand giving a thumbs up (thumb pointing up, other fingers curled).
- slower: ONE flat hand (the other hand down), palm facing the camera, all fingers pointing up, held still like a stop sign.
- volume_down: ONE flat hand (the other hand down), palm facing straight down, held horizontal or pushing downward (not touching/leaving the chin).
- volume_up: ONE flat hand (the other hand down), palm facing straight up, held horizontal or lifting upward.
- need_break: BOTH hands together making a T or a cross (sports time-out), see a) above.
Use "none" for ordinary movement: waving side-to-side, talking casually with the hands, adjusting hair, glasses or headphones, scratching the nose, a curled fist resting under the chin (thinking pose), hands at the neck, or holding a phone, cup or other object.

Answer with JSON only: {{"hands": "...", "neck": "none"|"chin_or_face"|"one_hand"|"both_hands"|"arm_or_cord", "eyes": "open"|"closed"|"covered"|"down"|"not_visible", "phone": "none"|"holding"|"using", "level": "none"|"watch"|"danger", "what": "...", "people": <number>, "instruction": "<one id above or none>"}}"""
GESTURES = {"volume_down": "Could you turn the volume down, please?", "volume_up": "Could you turn the volume up, please?",
            "slower": "Could you slow down, please?", "faster": "Could you go a bit faster?", "thank_you": "Thank you!",
            "need_break": "Can we stop for a break soon?", "all_good": "All good back here."}
# The fields are generated in order, with no thinking, so every category comes after the description
# it depends on: "hands" first, then "neck", ..., "level". Measured with tools/eval_watch.py: with
# "neck" before "hands", Nemotron described "both hands clasped around the neck" and still answered
# one hand, and with "level" first it called both hands on the throat "none".
NECK = ["none", "chin_or_face", "one_hand", "both_hands", "arm_or_cord"]
NECK_THREAT = {"both_hands", "arm_or_cord"}   # a threat alert whatever "level" says
EYES = ["open", "closed", "covered", "down", "not_visible"]
EYES_SHUT = {"closed", "covered"}   # the driver can't see the road
PHONE = ["none", "holding", "using"]
WATCH_SCHEMA = {"type": "object", "properties": {"hands": {"type": "string"}, "neck": {"type": "string", "enum": NECK},
                "eyes": {"type": "string", "enum": EYES}, "phone": {"type": "string", "enum": PHONE},
                "level": {"type": "string", "enum": ["none", "watch", "danger"]}, "what": {"type": "string"}, "people": {"type": "integer"},
                "instruction": {"type": "string", "enum": ["none", *GESTURES]}},
                "required": ["hands", "neck", "eyes", "phone", "level", "what", "people", "instruction"]}

def car_actions(v):
    # What the car does with a verdict, decided in code from the fields rather than left to the model's
    # "level" (it didn't always follow its own neck rules). watch_loop acts on it; tools/eval_watch.py scores it.
    return {"threat": v.get("level") == "danger" or v.get("neck") in NECK_THREAT,   # silent alert to the contacts
            "eyes": v.get("eyes") in EYES_SHUT,                                       # spoken warning, alert after 10 s
            "phone": v.get("phone") == "using",                                       # spoken reminder
            "sign": v.get("instruction") if v.get("instruction") in GESTURES else "none"}

def watch_intro(n, span):
    # Sent after WATCH_PROMPT: how the frames are ordered.
    return (f"{n} frames from the last {span} s, oldest first, newest last. Judge danger on the newest frame,"
            " and count a hand sign if it is clearly made across the frames (moving signs like thank_you, volume_down, volume_up"
            " or faster may start in an earlier frame and move down by the last)." if n > 1 else "One frame.")
# ---- end of watch prompt ----
MAX_FRAME_AGE_S = 10
HUB_LEVEL = {"none": "low", "watch": "medium", "danger": "high"}   # hub.py alerts on "high" only

def speak_aside(text, title, on_start=None, color="fg"):
    # Say something that isn't an answer to a press (a back-seat sign), unless the driver is
    # talking to the car right now. It becomes the current "session", so a press interrupts it.
    # on_start() runs once it's decided the car will speak (say() blocks until the speech ends).
    with lock:
        if sessions.get("driver") and sessions["driver"]["listening"]: return False
        s = sessions["driver"] = {"id": "aside", "board": "driver", "sid": None, "chunks": {}, "listening": False, "started": time.time()}
    if on_start: on_start()
    say(s, text, title, text, color)
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
# At most one request every WATCH_MIN_INTERVAL_S: when the endpoint answers in 1 s, unpaced requests
# (3 frames each, ~100 KB/s up the hotspot) took the Wi-Fi from the speaker's audio.
WATCH_MIN_INTERVAL_S = float(os.environ.get("WATCH_MIN_INTERVAL_S", "2"))
# Two tiers: the Uno Q's on-device person detection (Edge Impulse) watches all the time; Nemotron is
# asked at full rate while anyone might be in the car. Only after the Uno Q has seen nobody for
# EMPTY_PAUSE_S does it drop to one check every WATCH_EMPTY_INTERVAL_S (fail-open: a person the small
# model misses is still seen by Nemotron within that time, and a detector that's down changes nothing).
EMPTY_PAUSE_S, WATCH_EMPTY_INTERVAL_S = 10, 15
# car/vision/pause {"for_s": n}: no verdicts for n s (at most WATCH_PAUSE_MAX_S), while tools/eval_watch.py records.
WATCH_PAUSE_MAX_S = 120
watch_pause = {"until": 0.0}
# Eyes closed or covered (hands, an object, a hoodie): the driver may be falling asleep or passing out,
# or can't see the road. Not silent, unlike a threat: one burst with the eyes shut or covered in all 3
# frames (~1.5 s, longer than any blink) makes the car say so out loud,
# again on every verdict while they stay shut. Shut for EYES_ALERT_S (the warnings didn't wake them),
# the hub alerts the contacts (car/vision/unresponsive). Eyes seen open end it; a gap of more than
# EYES_GAP_S with no closed or covered verdict (camera dark, face not visible) starts the count again.
EYES_ALERT_S = float(os.environ.get("EYES_ALERT_S", "10"))
EYES_GAP_S = 8
EYES_WARN = {"closed": ["Open your eyes! Are you OK?", "Wake up! Open your eyes, or I'll alert your contacts."],
             "covered": ["I can't see your eyes. Uncover them! Are you OK?", "Uncover your eyes, or I'll alert your contacts."]}

def cabin_empty():
    o = cabin_people
    if o.get("people") is None or time.time() - float(o.get("at") or 0) > 10: return False   # can't tell
    return o["people"] == 0 and float(o.get("empty_for_s") or 0) + (time.time() - float(o["at"])) >= EMPTY_PAUSE_S

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

def watch_eyes(e, verdict, captured):
    # One eyes-closed episode: warn out loud, then alert. True when the car spoke.
    if verdict["eyes"] == "open" or (e["since"] and captured - e["last"] > EYES_GAP_S):
        if e["since"]: log(f"eyes: episode over after {e['last'] - e['since']:.1f} s")
        e.update(since=None, warned=0, alerted=False)
    how = verdict["eyes"]
    if how not in EYES_SHUT: return False
    if e["since"] is None: e["since"] = captured - WATCH_SPAN_S   # shut or covered in every frame of the burst
    e["last"] = captured
    shut = captured - e["since"]
    log(f"eyes: {how} for {shut:.1f} s")
    if shut >= EYES_ALERT_S and not e["alerted"]:
        e["alerted"] = True
        client.publish("car/vision/unresponsive", json.dumps({**verdict, "closed_s": round(shut, 1)}), qos=1)   # hub alerts the contacts
        return speak_aside(f"Your eyes have been {how} for too long. I'm alerting your contacts.", "Alerting contacts", color="warn")
    if e["alerted"]: return False
    text = EYES_WARN[how][min(e["warned"], len(EYES_WARN[how]) - 1)]
    e["warned"] += 1
    return speak_aside(text, "Eyes " + how, color="warn")

# Driver using a phone (looking at it, typing, or on a call held to the ear): the car asks them to put
# it down, once Nemotron has seen it in 2 of its last 3 verdicts (a glance at a phone doesn't count),
# and again every PHONE_REMIND_S while it goes on. Only a reminder: nobody else is alerted.
# phone_warn: false in secrets.json turns it off (a demo where the "driver" shows the live view on a phone).
PHONE_WARN = os.environ.get("PHONE_WARN", "true").lower() not in ("false", "0", "no", "off")
PHONE_REMIND_S = 20
PHONE_SAY = "Please put the phone down and keep your eyes on the road. Press the voice button and I'll send messages for you."

def watch_phone(p, verdict):
    # True when the car spoke.
    p["recent"].append(verdict["phone"] == "using")
    if not PHONE_WARN or sum(p["recent"]) < 2 or not p["recent"][-1]:
        if not any(p["recent"]): p["said_at"] = 0.0   # gone from the last 3: the next use gets a fresh reminder
        return False
    if time.time() - p["said_at"] < PHONE_REMIND_S: return False
    p["said_at"] = time.time()
    log("phone: the driver is using a phone, asking them to put it down")
    return speak_aside(PHONE_SAY, "Phone down", color="warn")

def watch_loop():
    import base64
    if not VERDA_KEY:
        log("Nemotron off: no VERDA_INFERENCE_KEY"); return
    recent_gestures, spoken_gesture = collections.deque(maxlen=3), None
    eyes = {"since": None, "last": 0.0, "warned": 0, "alerted": False}   # the current eyes-closed episode
    phone = {"recent": collections.deque(maxlen=3), "said_at": 0.0}
    last_ask, was_empty, was_paused = 0.0, False, False
    while True:
        try:
            while True:   # quiet while the car speaks, paced, and slow while the Uno Q sees an empty cabin
                with lock: busy = speaking["n"] or time.monotonic() < speaking["until"]
                empty = cabin_empty()
                if empty != was_empty:
                    was_empty = empty
                    log(f"cabin empty (on-device): Nemotron every {WATCH_EMPTY_INTERVAL_S} s" if empty else
                        "someone in the cabin (on-device): Nemotron back to full rate")
                paused = time.time() < watch_pause["until"]
                if paused != was_paused:
                    was_paused = paused
                    log("cabin watch paused: recording evaluation scenes (car/vision/pause)" if paused else "cabin watch resumed")
                gap = WATCH_EMPTY_INTERVAL_S if empty else WATCH_MIN_INTERVAL_S
                if not busy and not paused and time.monotonic() - last_ask >= gap: break
                time.sleep(0.2)
            try: frames, captured = camera_frames()
            except (urllib.error.HTTPError, OSError) as e:
                camera_state("off", e); time.sleep(5); continue
            if time.time() - captured > MAX_FRAME_AGE_S: camera_state("off", "frames too old"); time.sleep(2); continue
            if covered(frames[-1]): camera_state("dark"); time.sleep(2); continue   # no point asking about a black frame
            camera_state("ok")
            t = time.time(); last_ask = time.monotonic()
            client.publish("car/vision/sending", json.dumps({"at": round(t, 2)}))   # frames leaving the car: the UNO Q shows it
            intro = watch_intro(len(frames), WATCH_SPAN_S)
            text = nemotron([{"type": "text", "text": WATCH_PROMPT + "\n\n" + intro},
                             *({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode()}} for f in frames)],
                            schema=WATCH_SCHEMA)
            m = re.search(r"\{.*\}", text, re.S)
            v = json.loads(m.group(0)) if m else {"level": "none", "what": "", "people": 0}
            level = v.get("level") if v.get("level") in ("none", "watch", "danger") else "none"
            acts = car_actions(v)
            if acts["threat"]: level = "danger"   # hands, an arm or a cord on the throat: hub.py alerts on "high"
            took = time.time() - t
            gesture = v.get("instruction") if v.get("instruction") in GESTURES else "none"
            log(f"Nemotron ({took:.0f}s, frame {t - captured:.1f}s old): {level} neck={v.get('neck')} eyes={v.get('eyes')} phone={v.get('phone')} {v.get('what', '')!r} people={v.get('people')} sign={gesture} hands={str(v.get('hands', ''))[:120]!r}")
            verdict = {"level": HUB_LEVEL[level], "what": str(v.get("what", ""))[:200], "people": v.get("people"),
                       "hands": str(v.get("hands", ""))[:200], "neck": v.get("neck") if v.get("neck") in NECK else None,
                       "eyes": v.get("eyes") if v.get("eyes") in EYES else None,
                       "phone": v.get("phone") if v.get("phone") in PHONE else None, "instruction": gesture, "captured_at": captured, "took_s": round(took, 1), "model": NEMOTRON_MODEL}
            client.publish("car/vision/nemotron", json.dumps(verdict), qos=0, retain=True)
            last_verdict.clear(); last_verdict.update(verdict)
            if level == "danger":
                client.publish("car/vision/threat", json.dumps(verdict), qos=1)   # hub alerts silently
            if watch_eyes(eyes, verdict, captured): continue   # the car spoke: no hand sign on top of it
            if watch_phone(phone, verdict): continue
            # Hand sign: spoken once it's in 2 of the last 3 replies when the endpoint is fast, or
            # on a single reply when Nemotron took >= 5 s (so a busy shared endpoint doesn't make
            # someone hold a sign for a full minute).
            recent_gestures.append(gesture)
            slow = took >= 5.0
            if gesture != "none" and recent_gestures.count(gesture) >= (1 if slow else 2) and gesture != spoken_gesture:
                said = ("From the back seat: " if GESTURE_FROM == "back" else "Hand sign: ") + GESTURES[gesture]
                # car/vision/said: the live view marks the verdict that made the car speak
                if speak_aside(said, "Hand sign", lambda: client.publish("car/vision/said", json.dumps(
                        {"sign": gesture, "text": said, "captured_at": captured, "at": round(time.time(), 2)}))):
                    spoken_gesture = gesture
            if (gesture != spoken_gesture if slow else spoken_gesture not in recent_gestures): spoken_gesture = None
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
threading.Thread(target=load_multilingual, daemon=True).start()
log("voice ready" + ("" if GEMINI_KEY else " (no Gemini key: 'ask Google' is off)"))
while True: time.sleep(3600)
