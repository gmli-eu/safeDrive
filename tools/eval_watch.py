# Evaluate Nemotron's cabin watch on recorded, labelled camera bursts (dev machine, standard library
# plus ImageMagick's `convert` or Pillow to shrink frames).
#   python3 tools/eval_watch.py record [CASE ...] [--reps N]   act out each scene in front of the UNO Q camera
#   python3 tools/eval_watch.py run [--rev REV] [--name NAME] [--only CASE,...] [--exclude CASE,...] [--workers N]
#   python3 tools/eval_watch.py compare RESULT.json RESULT.json ... [--exclude CASE,...]
#   python3 tools/eval_watch.py cases                          list the scenes and what each should give
#
# record: saves bursts of 3 frames, 0.75 s apart (the 1.5 s the cabin watch sends), shrunk to 640 px
#   like voice.py's shrink(), in eval/data/<case>/<time>/ (ignored by git: they show people's faces).
#   While it records, it pauses the car's own cabin watch (car/vision/pause), so a staged choking
#   or closed-eyes scene doesn't alert anyone. Delete a burst's folder if the scene went wrong.
# run: sends every burst to Nemotron with the prompt, schema and request voice.py uses (the block
#   between the "watch prompt" markers in jetson-hub/voice.py, at the working tree or at git REV),
#   scores what the car would do with each verdict, and writes eval/results/<date>-<name>.json.
# compare: one table, a column per run, on the bursts all the runs have in common.
import base64, concurrent.futures as cf, json, os, pathlib, re, shutil, socket, statistics, struct, subprocess, sys, threading, time
import urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("EVAL_DATA", ROOT / "eval" / "data"))            # recorded bursts (not in git)
RESULTS = pathlib.Path(os.environ.get("EVAL_RESULTS", ROOT / "eval" / "results"))   # scores (in git)
VOICE = "jetson-hub/voice.py"
CAMERA = os.environ.get("CAMERA_URL", "http://172.20.10.4:8090") + "/frame.jpg"   # the UNO Q (unoq-camera/stream.py)
MQTT_HOST = os.environ.get("MQTT_HOST", "172.20.10.2")                         # the Jetson's broker
FRAMES, GAP_S, WIDTH = 3, 0.75, 640          # as WATCH_FRAMES, WATCH_SPAN_S and WATCH_WIDTH in voice.py

# What the car should do with each scene. Every scene is checked on every task: a thumbs up must not
# raise a threat alert, and choking must not be read as a hand sign.
#   threat: level "danger" or hands/arm/cord on the throat (silent alert)    eyes: eyes "closed" or "covered" (spoken warning)
#   phone:  phone "using" (spoken reminder)                  sign: the hand sign the car would speak
CASES = {
    "normal":          ("Sit normally and look ahead, as if driving", {}),
    "empty":           ("Leave the camera's view: nobody in the picture", {}),
    "talking":         ("Talk and move your hands casually, as in a conversation", {}),
    "chin_rest":       ("Rest your chin on your fist (thinking pose)", {}),
    "scratch_neck":    ("Scratch the side of your neck with one hand", {}),
    "choke_self":      ("Clutch your own throat with both hands (the choking sign)", {"threat": True}),
    "neck_from_behind": ("Reach behind your head and wrap the fingers of both hands around the sides of your neck,"
                         " as if someone behind the seat were grabbing it", {"threat": True}),
    "ligature":        ("Hold a scarf, belt or cable across the front of your throat, both ends pulled back behind your head"
                        " (not tight!)", {"threat": True}),
    "knife":           ("Hold a knife (a kitchen knife or a toy) raised, point up, near your face", {"threat": True}),
    "holding_object":  ("Hold a pen or a spoon raised the same way, near your face", {}),
    # A second person is needed: only recorded when named (record choke_behind arm_neck).
    "choke_behind":    ("Someone behind you puts both hands around your neck (gently!)", {"threat": True, "pair": True}),
    "arm_neck":        ("Someone behind you puts a forearm across your throat (gently!)", {"threat": True, "pair": True}),
    "eyes_closed":     ("Close your eyes and keep them closed, head up", {"eyes": True}),
    "eyes_slumped":    ("Close your eyes and let your head drop forward or to the side", {"eyes": True}),
    "eyes_hands":      ("Cover your eyes with your hands", {"eyes": True}),
    "eyes_object":     ("Hold an object (a cap, a book, a cloth) over your eyes", {"eyes": True}),
    "eyes_hood":       ("Pull a hood or hat down over your eyes (skip if you have none)", {"eyes": True}),
    "looking_down":    ("Look down at your lap or a laptop, eyes open", {}),
    "blinking":        ("Look ahead and blink a few times normally", {}),
    "sunglasses":      ("Wear sunglasses and look ahead (skip if you have none)", {}),
    "phone_scroll":    ("Hold a phone and scroll or type on it, looking at it", {"phone": True}),
    "phone_call":      ("Hold a phone to your ear, as in a call", {"phone": True}),
    "phone_hold":      ("Hold a phone in your hand but look ahead, not at it", {}),
    "sign_all_good":   ("Thumbs up, held steady", {"sign": "all_good"}),
    "sign_need_break": ("Both hands make a T (time-out)", {"sign": "need_break"}),
    "sign_volume_down": ("One flat hand, palm down, pushing downward", {"sign": "volume_down"}),
    "sign_volume_up":  ("One flat hand, palm up, lifting upward", {"sign": "volume_up"}),
    "sign_slower":     ("One flat hand, palm to the camera, fingers up, like a stop sign", {"sign": "slower"}),
    "sign_faster":     ("Index finger up, circling", {"sign": "faster"}),
    "sign_thank_you":  ("Flat hand at your chin moving forward and down", {"sign": "thank_you"}),
}
TASKS = ("threat", "eyes", "phone")

def expected(case):
    e = CASES[case][1]
    return {"threat": bool(e.get("threat")), "eyes": bool(e.get("eyes")), "phone": bool(e.get("phone")), "sign": e.get("sign", "none")}

def predicted(v):
    # What the car does with a verdict, for prompt versions without car_actions() in their block
    # (newer ones bring their own: the car's exact rules).
    return {"threat": v.get("level") == "danger", "eyes": v.get("eyes") in ("closed", "covered"),
            "phone": v.get("phone") == "using", "sign": v.get("instruction") or "none"}

# ---------- record ----------
def shrink(jpg):
    # voice.py's shrink(): fit into 640 x 640, JPEG quality 75.
    if shutil.which("convert"):
        return subprocess.run(["convert", "jpg:-", "-resize", f"{WIDTH}x{WIDTH}>", "-quality", "75", "jpg:-"],
                              input=jpg, capture_output=True, check=True).stdout
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(jpg)); im.thumbnail((WIDTH, WIDTH))
    out = io.BytesIO(); im.convert("RGB").save(out, "JPEG", quality=75); return out.getvalue()

def mqtt_publish(topic, payload):
    # One QoS 0 message on the Jetson's broker (no paho needed; see tools/mqtt_peek.py).
    def s(x): x = x.encode() if isinstance(x, str) else x; return struct.pack("!H", len(x)) + x
    def pkt(t, body):
        n, ln = len(body), b""
        while True:
            b, n = n % 128, n // 128; ln += bytes([b | (0x80 if n else 0)])
            if not n: break
        return bytes([t]) + ln + body
    with socket.create_connection((MQTT_HOST, 1883), 5) as sock:
        sock.sendall(pkt(0x10, s("MQTT") + b"\x04\x02" + struct.pack("!H", 30) + s(f"eval-watch-{os.getpid()}")))
        sock.recv(4)   # CONNACK
        sock.sendall(pkt(0x30, s(topic) + json.dumps(payload).encode()) + b"\xe0\x00")   # PUBLISH, DISCONNECT

class Pause:
    # Keeps the car's cabin watch paused while recording (voice.py caps each pause at 120 s).
    def __enter__(self):
        self.stop = threading.Event()
        def renew():
            while not self.stop.is_set():
                try: mqtt_publish("car/vision/pause", {"for_s": 90})
                except OSError as e: print(f"  (couldn't pause the car's cabin watch: {e})")
                self.stop.wait(30)
        threading.Thread(target=renew, daemon=True).start()
        return self
    def __exit__(self, *a):
        self.stop.set()
        try: mqtt_publish("car/vision/pause", {"for_s": 0}); print("car's cabin watch resumed")
        except OSError as e: print(f"couldn't resume the car's cabin watch ({e}): it resumes by itself within 90 s")

def grab():
    with urllib.request.urlopen(CAMERA, timeout=5) as r: return r.read()

def record(cases, reps):
    unknown = [c for c in cases if c not in CASES]
    if unknown: sys.exit(f"unknown case(s): {', '.join(unknown)}. See: python3 tools/eval_watch.py cases")
    cases = cases or [c for c in CASES if not CASES[c][1].get("pair")]   # the two-person scenes only when named
    grab()   # fail early if the camera isn't reachable
    print(f"Recording {reps} bursts per scene, {len(cases)} scenes. The car's cabin watch is paused meanwhile.\n"
          "Hold each scene steady from the countdown until it says done. Enter = record, s = skip, q = quit.")
    with Pause():
        for i, case in enumerate(cases, 1):
            have = len(list((DATA / case).glob("*/f0.jpg"))) if (DATA / case).exists() else 0
            a = input(f"\n[{i}/{len(cases)}] {case} ({have} recorded): {CASES[case][0]}\n  > ").strip().lower()
            if a == "q": break
            if a == "s": continue
            for n in (3, 2, 1): print(f"  {n}...", end=" ", flush=True); time.sleep(1)
            print()
            for r in range(reps):
                folder = DATA / case / f"{time.strftime('%Y%m%d-%H%M%S')}-{r}"
                folder.mkdir(parents=True, exist_ok=True)
                for f in range(FRAMES):
                    t = time.monotonic()
                    (folder / f"f{f}.jpg").write_bytes(shrink(grab()))
                    if f < FRAMES - 1: time.sleep(max(0.0, GAP_S - (time.monotonic() - t)))
                print(f"  burst {r + 1}/{reps} saved", flush=True)
                time.sleep(0.4)
            print("  done")

# ---------- run ----------
def watch_block(rev):
    # The prompt block of voice.py, at the working tree (rev None) or at a git revision. Older
    # revisions have no markers: there it runs from GESTURE_FROM to MAX_FRAME_AGE_S.
    src = (ROOT / VOICE).read_text() if not rev else subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{rev}:{VOICE}"], capture_output=True, text=True, check=True).stdout
    m = (re.search(r"^# ---- watch prompt.*?$(.*?)^# ---- end of watch prompt", src, re.S | re.M)
         or re.search(r"^(GESTURE_FROM = .*?)^MAX_FRAME_AGE_S", src, re.S | re.M))
    if not m: sys.exit(f"no watch prompt block in {VOICE}{' at ' + rev if rev else ''}")
    ns = {"os": os}
    exec(m.group(1), ns)
    if "watch_intro" not in ns:   # older revisions built it inside watch_loop
        ns["watch_intro"] = lambda n, span: (f"{n} frames from the last {span} s, oldest first, newest last. Judge danger on the newest frame,"
            " and count a hand sign if it is clearly made across the frames (moving signs like thank_you, volume_down, volume_up"
            " or faster may start in an earlier frame and move down by the last)." if n > 1 else "One frame.")
    url = re.search(r'NEMOTRON_URL = os.environ.get\("NEMOTRON_URL", "([^"]+)"\)', src)
    model = re.search(r'NEMOTRON_MODEL = os.environ.get\("NEMOTRON_MODEL", "([^"]+)"\)', src)
    return ns, os.environ.get("NEMOTRON_URL", url.group(1)), os.environ.get("NEMOTRON_MODEL", model.group(1))

def verda_key():
    if os.environ.get("VERDA_INFERENCE_KEY"): return os.environ["VERDA_INFERENCE_KEY"]
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r"^VERDA_INFERENCE_KEY=(.+)$", env.read_text(), re.M)
        if m: return m.group(1).strip().strip('"\'')
    sec = ROOT / "jetson-hub" / "secrets.json"
    if sec.exists() and json.loads(sec.read_text()).get("verda_inference_key"): return json.loads(sec.read_text())["verda_inference_key"]
    sys.exit("no Verda key: set VERDA_INFERENCE_KEY in .env or verda_inference_key in jetson-hub/secrets.json")

def ask(ns, url, model, key, frames):
    # voice.py's nemotron() with the watch prompt, with retries: the endpoint is shared with other teams.
    content = [{"type": "text", "text": ns["WATCH_PROMPT"] + "\n\n" + ns["watch_intro"](len(frames), GAP_S * (FRAMES - 1))},
               *({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode()}} for f in frames)]
    body = {"model": model, "max_tokens": 300, "temperature": 0, "messages": [{"role": "user", "content": content}],
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": ns["WATCH_SCHEMA"]}}}
    for attempt in range(4):
        t = time.time()
        try:
            req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r: res = json.load(r)
            text = res["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", text, re.S)
            return (json.loads(m.group(0)) if m else {}), time.time() - t, None
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            err = repr(e)[:200]
            time.sleep(2 * 2 ** attempt)
    return {}, None, err

def bursts(only, exclude=()):
    out = []
    for case in sorted(p.name for p in DATA.iterdir() if p.is_dir()) if DATA.exists() else []:
        if case not in CASES: print(f"skipping eval/data/{case}: not a known case"); continue
        if (only and case not in only) or case in exclude: continue
        for b in sorted((DATA / case).iterdir()):
            fr = [b / f"f{i}.jpg" for i in range(FRAMES)]
            if all(f.exists() for f in fr): out.append((case, b.name, [f.read_bytes() for f in fr]))
    return out

def score(rows):
    # rows: [{"case", "burst", "expected", "predicted", "took_s", "error"}] -> summary
    ok = [r for r in rows if not r["error"]]
    s = {"bursts": len(rows), "errors": len(rows) - len(ok)}
    for task in TASKS:
        tp = sum(r["expected"][task] and r["predicted"][task] for r in ok)
        fn = sum(r["expected"][task] and not r["predicted"][task] for r in ok)
        fp = sum(not r["expected"][task] and r["predicted"][task] for r in ok)
        tn = len(ok) - tp - fn - fp
        s[task] = {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
                   "recall": tp / (tp + fn) if tp + fn else None, "false_alarm_rate": fp / (fp + tn) if fp + tn else None}
    signs = [r for r in ok if r["expected"]["sign"] != "none"]
    others = [r for r in ok if r["expected"]["sign"] == "none"]
    s["sign"] = {"correct": sum(r["predicted"]["sign"] == r["expected"]["sign"] for r in signs), "of": len(signs),
                 "false_signs": sum(r["predicted"]["sign"] != "none" for r in others), "of_others": len(others),
                 "confusion": {}}
    for r in signs:
        row = s["sign"]["confusion"].setdefault(r["expected"]["sign"], {})
        row[r["predicted"]["sign"]] = row.get(r["predicted"]["sign"], 0) + 1
    took = sorted(r["took_s"] for r in ok)
    s["latency_s"] = {"median": statistics.median(took) if took else None,
                      "p90": took[min(len(took) - 1, int(0.9 * len(took)))] if took else None}
    s["per_case"] = {}
    for r in ok:
        c = s["per_case"].setdefault(r["case"], {"n": 0, "right": 0})
        c["n"] += 1; c["right"] += r["predicted"] == r["expected"]
    return s

def pct(x): return "-" if x is None else f"{100 * x:.0f} %"

def summary_lines(s):
    yield f"{s['bursts']} bursts, {s['errors']} failed requests. Median {s['latency_s']['median'] or 0:.1f} s per verdict, 90 % within {s['latency_s']['p90'] or 0:.1f} s."
    yield ""
    yield "| Task | Caught | Missed | Recall | False alarms | False-alarm rate |"
    yield "|---|---|---|---|---|---|"
    names = {"threat": "Threat alert (danger)", "eyes": "Eyes closed or covered", "phone": "Phone use"}
    for t in TASKS:
        x = s[t]
        yield f"| {names[t]} | {x['tp']} | {x['fn']} | {pct(x['recall'])} | {x['fp']} of {x['fp'] + x['tn']} | {pct(x['false_alarm_rate'])} |"
    g = s["sign"]
    yield f"| Hand signs | {g['correct']} of {g['of']} right | | {pct(g['correct'] / g['of'] if g['of'] else None)} | {g['false_signs']} of {g['of_others']} | {pct(g['false_signs'] / g['of_others'] if g['of_others'] else None)} |"
    yield ""
    yield "| Scene | Bursts fully right |"
    yield "|---|---|"
    for case in CASES:
        if case in s["per_case"]:
            c = s["per_case"][case]; yield f"| `{case}` | {c['right']} of {c['n']} |"

def run(rev, name, only, workers, exclude=()):
    ns, url, model = watch_block(rev)
    key, todo = verda_key(), bursts(only, exclude)
    if not todo: sys.exit("no recorded bursts in eval/data: python3 tools/eval_watch.py record")
    label = name or (rev or "working-tree")
    print(f"Asking {model} about {len(todo)} bursts with the prompt from {VOICE} at {rev or 'the working tree'}...")
    rows, done = [], 0
    def one(item):
        case, burst, frames = item
        v, took, err = ask(ns, url, model, key, frames)
        return {"case": case, "burst": burst, "expected": expected(case), "predicted": ns.get("car_actions", predicted)(v),
                "took_s": round(took, 2) if took else None, "error": err, "verdict": v}
    with cf.ThreadPoolExecutor(workers) as pool:
        for r in pool.map(one, todo):
            rows.append(r); done += 1
            wrong = r["error"] or ", ".join(f"{k}: {r['predicted'][k]} (want {r['expected'][k]})" for k in r["expected"]
                                            if r["predicted"][k] != r["expected"][k])
            print(f"  [{done}/{len(todo)}] {r['case']}/{r['burst']}: {'ok' if not wrong else wrong}", flush=True)
    s = score(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M')}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', label)}.json"
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", rev or "HEAD"], capture_output=True, text=True).stdout.strip()
    out.write_text(json.dumps({"name": label, "rev": rev, "commit": commit, "dirty": not rev and bool(subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", VOICE], capture_output=True, text=True).stdout.strip()),
        "model": model, "at": time.strftime("%Y-%m-%d %H:%M"), "summary": s,
        "rows": [{k: v for k, v in r.items()} for r in rows]}, indent=1, ensure_ascii=False))
    print("\n" + "\n".join(summary_lines(s)))
    misses = [r for r in rows if not r["error"] and r["predicted"] != r["expected"]]
    if misses:
        print("\nWrong verdicts (what Nemotron said):")
        for r in misses:
            v = r["verdict"]
            print(f"  {r['case']}/{r['burst']}: level={v.get('level')} neck={v.get('neck')} eyes={v.get('eyes')} phone={v.get('phone')}"
                  f" sign={v.get('instruction')} what={v.get('what')!r} hands={v.get('hands')!r}")
    print(f"\nsaved {out}")

# ---------- compare ----------
def compare(paths, exclude=()):
    runs = [json.loads(pathlib.Path(p).read_text()) for p in paths]
    for run_ in runs: run_["rows"] = [r for r in run_["rows"] if r["case"] not in exclude]
    common = set.intersection(*({(r["case"], r["burst"]) for r in run["rows"] if not r["error"]} for run in runs))
    sums = [score([r for r in run["rows"] if (r["case"], r["burst"]) in common and not r["error"]]) for run in runs]
    head = [f"{run['name']} ({run['commit']}{', edited' if run.get('dirty') else ''})" for run in runs]
    print(f"On the {len(common)} bursts every run answered:\n")
    print("| | " + " | ".join(head) + " |"); print("|---|" + "---|" * len(runs))
    names = {"threat": "Threat alert", "eyes": "Eyes closed or covered", "phone": "Phone use"}
    for t in TASKS:
        print(f"| {names[t]}: caught | " + " | ".join(f"{s[t]['tp']} of {s[t]['tp'] + s[t]['fn']} ({pct(s[t]['recall'])})" for s in sums) + " |")
        print(f"| {names[t]}: false alarms | " + " | ".join(f"{s[t]['fp']} of {s[t]['fp'] + s[t]['tn']}" for s in sums) + " |")
    print("| Hand signs right | " + " | ".join(f"{s['sign']['correct']} of {s['sign']['of']}" for s in sums) + " |")
    print("| Signs where there was none | " + " | ".join(f"{s['sign']['false_signs']} of {s['sign']['of_others']}" for s in sums) + " |")
    print("| Median time per verdict | " + " | ".join(f"{s['latency_s']['median'] or 0:.1f} s" for s in sums) + " |")

def main():
    a = sys.argv[1:]
    def opt(name, default=None):
        if name in a:
            i = a.index(name); v = a[i + 1]; del a[i:i + 2]; return v
        return default
    cmd = a.pop(0) if a else "help"
    if cmd == "record":
        reps = int(opt("--reps", "5")); record(a, reps)
    elif cmd == "run":
        rev, name, only, workers = opt("--rev"), opt("--name"), opt("--only"), int(opt("--workers", "3"))
        exclude = set((opt("--exclude") or "").split(",")) - {""}
        run(rev, name, set(only.split(",")) if only else None, workers, exclude)
    elif cmd == "compare" and len(a) > 0:
        exclude = set((opt("--exclude") or "").split(",")) - {""}
        compare(a, exclude)
    elif cmd == "cases":
        for c, (how, _) in CASES.items():
            e = expected(c); want = [t for t in TASKS if e[t]] + ([f"sign {e['sign']}"] if e["sign"] != "none" else [])
            print(f"  {c:17} {', '.join(want) or 'nothing':20} {how}" + ("  [second person, only when named]" if CASES[c][1].get("pair") else ""))
    else: print(__doc__ or open(__file__).read().split("\nimport")[0])

if __name__ == "__main__": main()
