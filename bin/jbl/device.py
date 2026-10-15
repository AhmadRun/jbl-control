"""One JBL device: Fast Pair facts in, BLE control link, merged state out.

The session owns a control thread. That thread opens the ATT link to the buds'
LE address, turns notifications on, reads every feature's current value (the
same reads double as the capability probe), then serves queued commands with
pacing, because frames written back to back get dropped by the firmware.

Frames from the buds arrive on the ATT reader thread; they are decoded and
merged into the state there and also handed to the control thread so it can
tell an answered probe from a silent one.
"""
import copy
import json
import os
import queue
import threading
import time

from . import protocol as p

PACE = 0.45          # seconds between writes
REPLY_TIMEOUT = 1.6  # seconds to wait for a probe answer
STALE_COMMAND = 20   # seconds a command may wait for the link

# feature -> (request frame factory, predicate on a reply frame)
PROBES = (
    ("modes", p.mode_get, lambda f: f["cmd"] == p.CMD_ANC and f["payload"][:1] == b"\x12"),
    ("ancLevel", p.anc_level_get,
     lambda f: f["cmd"] == p.CMD_ANC and f["payload"][:1] == b"\x22" and b"\x04" in f["payload"][1::2]),
    ("info", p.info_get, lambda f: f["cmd"] == p.CMD_INFO and f["payload"][:1] == b"\x02"),
    ("eq", p.eq_get, lambda f: f["cmd"] == p.CMD_EQ and len(f["payload"]) >= 16),
    ("voicePrompts", p.voice_prompts_get, lambda f: f["cmd"] == p.CMD_VOICE_PROMPT and f["payload"][:1] == b"\x05"),
    ("voiceAware", p.voice_aware_get, lambda f: f["cmd"] == p.CMD_VOICE_AWARE and len(f["payload"]) >= 2),
    ("findBuds", p.find_get, lambda f: f["cmd"] == p.CMD_FIND and len(f["payload"]) >= 3),
    ("serial", p.serial_get, lambda f: f["cmd"] == p.CMD_SERIAL),
    ("lowLatency", p.sav_get, lambda f: f["cmd"] == p.CMD_SAV and p.sav_mode(f["raw"]) is not None),
)

SLOT_MODES = {1: "anc", 2: "ambient", 3: "talkthru"}
MODE_LABELS = {"off": "Off", "anc": "Noise Cancelling", "ambient": "Ambient Aware", "talkthru": "TalkThru"}


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "jbl-control", "support.json")


def load_cache():
    try:
        with open(cache_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(data):
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v


class Session:
    def __init__(self, address, name, emit, link_factory, log=None, pace=PACE,
                 reply_timeout=REPLY_TIMEOUT, use_cache=True):
        self.address = address
        self.emit = emit                  # emit(state_dict), any thread
        self.link_factory = link_factory  # (le_address, on_bytes, on_close) -> link
        self.log = log or (lambda *a: None)
        self.pace = pace
        self.reply_timeout = reply_timeout
        self.use_cache = use_cache
        self.lock = threading.RLock()
        self.state = {
            "connected": True, "address": address, "name": name or "JBL",
            "control": "waiting", "battery": {}, "features": {},
        }
        self.reader = p.FrameReader()
        self.replies = queue.Queue()
        self.commands = queue.Queue()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.link = None
        self.le_address = None
        self.on_control_failed = None     # callback(session) after a link failure
        self._last_write = 0.0

    # ------------------------------------------------------------ state

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def update(self, fragment):
        if not fragment:
            return
        with self.lock:
            before = json.dumps(self.state, sort_keys=True)
            fragment = dict(fragment)
            battery = fragment.pop("battery", None)
            if battery:
                for k, v in battery.items():
                    # A missing reading never overwrites a real one from the other source.
                    if v is not None or k not in self.state["battery"]:
                        self.state["battery"][k] = v
            merge(self.state, fragment)
            changed = json.dumps(self.state, sort_keys=True) != before
            snap = copy.deepcopy(self.state) if changed else None
        if changed:
            self.emit(snap)

    def set_features(self, features):
        with self.lock:
            self.state["features"] = dict(features)
        self.emit(self.snapshot())

    # ------------------------------------------------------------ lifecycle

    def start_control(self, le_address):
        if self.thread and self.thread.is_alive():
            if le_address == self.le_address:
                return
            self.le_address = le_address
            if self.state.get("control") != "ready":
                self.wake.set()  # retry now on the new address
            return
        self.le_address = le_address
        self.thread = threading.Thread(target=self._run, name="jbl-control", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        self.commands.put(None)
        self._drop_link()

    def _drop_link(self):
        link, self.link = self.link, None
        if link:
            try:
                link.close()
            except OSError:
                pass

    # ------------------------------------------------------------ link I/O

    def _on_bytes(self, data):
        mtu = getattr(self.link, "mtu", None)
        for frame in self.reader.feed(data, last=bool(mtu) and len(data) < mtu - 3):
            self._on_frame(frame)

    def _on_frame(self, frame):
        self.log("rx", frame["raw"].hex(" "), "(truncated)" if frame["truncated"] else "")
        fragment = p.decode(frame)
        if fragment.get("audioMode") not in (None, "video"):
            fragment["normalAudioMode"] = fragment["audioMode"]
        if "modeSlots" in fragment:
            fragment["features"] = {"modes": ["off"] + [SLOT_MODES[s] for s in fragment.pop("modeSlots")
                                                        if s in SLOT_MODES]}
        self.update(fragment)
        self.replies.put(frame)

    def _send(self, frame):
        wait = self.pace - (time.monotonic() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self.log("tx", frame.hex(" "))
        self.link.send(frame)
        self._last_write = time.monotonic()

    def _await(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                for frame in self.reader.flush():
                    self._on_frame(frame)
                    if predicate(frame):
                        return frame
                return None
            try:
                frame = self.replies.get(timeout=left)
            except queue.Empty:
                continue
            if predicate(frame):
                return frame

    # ------------------------------------------------------------ control thread

    def _run(self):
        failures = 0
        while not self.stopping.is_set():
            self.wake.clear()
            address = self.le_address
            try:
                self.update({"control": "connecting", "leAddress": address})
                self.link = self.link_factory(address, self._on_bytes, self._on_link_closed)
                failures = 0
                self._session()
            except OSError as e:
                self.log("control link %s:" % address, e)
                failures += 1
            finally:
                self._drop_link()
            if self.stopping.is_set():
                break
            self.update({"control": "unavailable" if failures > 2 else "reconnecting"})
            if self.on_control_failed and self.le_address == address:
                # The buds rotate their LE address; ask for the one they advertise now.
                self.on_control_failed(self)
            # A new address (start_control) wakes us straight away.
            self.wake.wait(min(60, 3 * max(1, failures)))

    def _on_link_closed(self):
        self.commands.put("__closed__")

    def _session(self):
        self.replies = queue.Queue()
        self._send(p.notify_on())
        time.sleep(0.3)
        self.probe()
        self.update({"control": "ready"})
        while not self.stopping.is_set():
            try:
                item = self.commands.get(timeout=5)
            except queue.Empty:
                if self.link is None or getattr(self.link, "closed", False):
                    raise OSError("control link closed")
                continue
            if item is None:
                return
            if item == "__closed__":
                raise OSError("control link closed")
            self._execute(item)

    def probe(self):
        cache = load_cache() if self.use_cache else {}
        model = self.state.get("model") or self.state.get("name")
        known = cache.get(model, {}).get("answered") if model else None
        answered = []
        features = dict(self.state.get("features", {}))
        info_fids = set()
        for name, factory, predicate in PROBES:
            if known is not None and name not in known:
                continue
            frame = None
            for _attempt in range(2):
                self._send(factory())
                frame = self._await(predicate, self.reply_timeout)
                if frame:
                    break
            if not frame:
                continue
            answered.append(name)
            if name == "info":
                info_fids = p.decode_info(frame["payload"])[1]
        snap = self.snapshot()
        features["modes"] = snap["features"].get("modes", features.get("modes", []))
        if "modes" not in answered:
            features["modes"] = []
        features.update({
            "ancLevel": "ancLevel" in answered,
            "eq": "eq" in answered,
            "voicePrompts": "voicePrompts" in answered,
            "voiceAware": "voiceAware" in answered,
            "findBuds": "findBuds" in answered,
            "lowLatency": "lowLatency" in answered,
            "autoOff": p.FID_AUTO_OFF in info_fids or "autoOff" in snap,
            "spatial": "spatial" in snap,
            "safeSound": p.FID_SAFE_SOUND in info_fids or "safeSound" in snap,
            "powerOff": True,
        })
        self.set_features(features)
        if model and self.use_cache and known is None:
            cache[model] = {"answered": answered}
            save_cache(cache)

    # ------------------------------------------------------------ commands

    def submit(self, op, *args, done=None):
        """Queue a command. done(error_or_None) runs on the control thread once
        the buds have answered (or not)."""
        self.commands.put((op, args, done, time.monotonic()))

    def _execute(self, item):
        op, args, done, queued = item
        if time.monotonic() - queued > STALE_COMMAND:
            # Queued while the link was down; replaying it now would surprise.
            if done:
                done("the buds were not reachable in time")
            return
        try:
            frame, optimistic, expect = self._build(op, args)
        except (ValueError, KeyError, IndexError) as e:
            message = self.error("%s: %s" % (op, e))
        else:
            self._send(frame)
            if optimistic:
                self.update(optimistic)
            reply = self._await(expect, self.reply_timeout) if expect else None
            message = self._verify(op, args, reply)
        if done:
            done(message)

    def _verify(self, op, args, reply):
        """Some settings are declined by the buds without an error: they simply
        report the old value back. Say so instead of showing what was asked for."""
        if reply is None:
            return None
        if op == "mode":
            actual = p.decode(reply).get("mode")
            if actual and actual != args[0]:
                return self.error("The buds stayed in %s. %s may need both buds in your ears."
                                  % (MODE_LABELS.get(actual, actual), MODE_LABELS.get(args[0], args[0])))
        elif op == "low-latency":
            if p.sav_mode(reply["raw"]) != ("video" if args[0] else self.state.get("normalAudioMode", "audio")):
                # The codec blob is model-specific; this model wants another one.
                self.update(p.decode(reply))
                self.demote("lowLatency")
                return self.error("This model does not accept JBL's low-latency setting.")
        return None

    def error(self, message):
        self.update({"lastError": message, "errorAt": time.time()})
        return message

    def demote(self, feature):
        with self.lock:
            self.state["features"][feature] = False
        self.emit(self.snapshot())
        model = self.state.get("model") or self.state.get("name")
        if model and self.use_cache:
            cache = load_cache()
            entry = cache.setdefault(model, {"answered": []})
            if feature in entry.get("answered", []):
                entry["answered"].remove(feature)
            save_cache(cache)

    def _build(self, op, args):
        s = self.snapshot()
        if op == "mode":
            # No optimistic update: the buds report the mode they actually took.
            return p.mode_set(args[0]), None, lambda f: f["cmd"] == p.CMD_ANC and f["payload"][:1] == b"\x12"
        if op == "anc-level":
            return p.anc_level_set(int(args[0])), {"ancLevel": max(1, min(7, int(args[0])))}, None
        if op == "eq-preset":
            cat = int(args[0])
            gains = list(p.EQ_PRESETS[cat][1])
            return p.eq_preset_set(cat), {"eq": {"preset": cat, "gains": gains}}, None
        if op == "eq-off":
            flat = [0.0] * len(p.EQ_BAND_FREQS)
            return p.eq_set(p.EQ_OFF, flat), {"eq": {"preset": "off", "gains": flat}}, None
        if op == "eq-custom":
            gains = [max(-6.0, min(6.0, float(g))) for g in args[0]]
            return p.eq_set(p.EQ_CUSTOM, gains), {"eq": {"preset": "custom", "gains": gains}}, None
        if op == "voice-prompts":
            return p.voice_prompts_set(args[0]), {"voicePrompts": bool(args[0])}, None
        if op == "voice-aware":
            level = max(0, min(255, int(args[0])))
            return (p.voice_aware_set(level, s.get("voiceAwareExtra", 0)), {"voiceAware": level},
                    lambda f: f["cmd"] == p.CMD_VOICE_AWARE)
        if op == "find":
            left, right = args
            finding = dict(s.get("finding") or {"left": False, "right": False})
            if left is not None:
                finding["left"] = bool(left)
            if right is not None:
                finding["right"] = bool(right)
            return p.find_set(left, right), {"finding": finding}, None
        if op == "gesture":
            gesture, action = args
            return p.gestures_set([(gesture, action)]), {"gestures": {gesture: action}}, None
        if op == "auto-off":
            enabled, minutes = args
            if minutes is None:
                minutes = (s.get("autoOff") or {}).get("minutes", 30)
            return (p.auto_off_set(enabled, minutes),
                    {"autoOff": {"enabled": bool(enabled), "minutes": int(minutes)}}, None)
        if op == "spatial":
            enabled, scene = args
            scene = scene or (s.get("spatial") or {}).get("scene", "music")
            return p.spatial_set(enabled, scene), {"spatial": {"enabled": bool(enabled), "scene": scene}}, None
        if op == "safe-sound":
            return p.safe_sound_set(args[0]), {"safeSound": bool(args[0])}, None
        if op == "low-latency":
            if not s["features"].get("lowLatency"):
                raise ValueError("not supported by this model")
            # Turning low latency off goes back to the device's own non-video blob.
            mode = "video" if args[0] else (s.get("normalAudioMode") or "audio")
            return p.sav_set(mode), None, lambda f: f["cmd"] == p.CMD_SAV
        if op == "raw":
            return p.raw_frame(args[0]), None, lambda f: True
        if op == "power-off":
            return p.power_off(), {"control": "powering-off"}, None
        raise ValueError("unknown command")
