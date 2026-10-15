"""The line commands the bridge accepts (from the widget, jblctl, keybindings).

    status                          print the current state once
    mode off|anc|ambient|talkthru
    mode cycle [m1,m2,...]          next mode in the list (default: all the device has)
    anc-level 1..7
    eq off
    eq preset <id|name>             jazz, vocal, bass, club, studio, extreme-bass
    eq custom g1,g2,...,g10         dB per band, 32 Hz .. 16 kHz, -6..6
    eq band <0-9> <gain>            change one band of the current curve
    voice-prompts on|off
    voice-aware <level>
    find left|right|both|stop
    gesture <gesture> <action>
    auto-off on|off [minutes]
    spatial on|off [movie|music|game]
    safe-sound on|off
    low-latency on|off
    power-off
    raw <hex>                       send a hand-written frame (debugging; reset stays blocked)
    select <address>                which connected JBL device to control
    refresh                         re-open the links and re-read everything
"""
from . import protocol as p

CYCLE_ORDER = ("anc", "ambient", "talkthru", "off")


class CommandError(ValueError):
    pass


def _bool(word):
    w = word.lower()
    if w in ("on", "true", "1", "yes", "enable", "enabled"):
        return True
    if w in ("off", "false", "0", "no", "disable", "disabled"):
        return False
    raise CommandError("expected on/off, got %r" % word)


def preset_id(word):
    w = word.lower().replace("_", "-").replace(" ", "-")
    if w.isdigit() and int(w) in p.EQ_PRESETS:
        return int(w)
    for cat, (label, _gains) in p.EQ_PRESETS.items():
        if label.lower().replace(" ", "-") == w:
            return cat
    raise CommandError("unknown EQ preset %r" % word)


def next_mode(current, available, wanted=None):
    order = [m for m in (wanted or CYCLE_ORDER) if m in available] or [m for m in CYCLE_ORDER if m in available]
    if not order:
        raise CommandError("this device has no listening modes")
    if current not in order:
        return order[0]
    return order[(order.index(current) + 1) % len(order)]


def parse(line, state=None):
    """Line -> (op, *args) for Session.submit, or a bridge-level op.

    `state` is the current device state, needed by the relative commands
    (mode cycle, eq band).
    """
    state = state or {}
    words = line.strip().split()
    if not words:
        raise CommandError("empty command")
    cmd, rest = words[0].lower(), words[1:]

    def need(n):
        if len(rest) < n:
            raise CommandError("%s: missing argument" % cmd)

    if cmd in ("status", "refresh", "power-off"):
        return (cmd,)
    if cmd == "select":
        need(1)
        return ("select", rest[0].upper())
    if cmd == "mode":
        need(1)
        available = (state.get("features") or {}).get("modes") or []
        if rest[0] == "cycle":
            wanted = rest[1].split(",") if len(rest) > 1 else None
            return ("mode", next_mode(state.get("mode"), available, wanted))
        if rest[0] not in p.MODES:
            raise CommandError("unknown mode %r" % rest[0])
        if available and rest[0] not in available:
            raise CommandError("this device has no %s mode" % rest[0])
        return ("mode", rest[0])
    if cmd == "anc-level":
        need(1)
        return ("anc-level", int(rest[0]))
    if cmd == "eq":
        need(1)
        if rest[0] == "off":
            return ("eq-off",)
        if rest[0] == "preset":
            need(2)
            if rest[1].lower() == "off":
                return ("eq-off",)
            return ("eq-preset", preset_id(rest[1]))
        if rest[0] == "custom":
            need(2)
            gains = [float(g) for g in rest[1].split(",")]
            if len(gains) != len(p.EQ_BAND_FREQS):
                raise CommandError("eq custom needs %d gains" % len(p.EQ_BAND_FREQS))
            return ("eq-custom", gains)
        if rest[0] == "band":
            need(3)
            gains = list((state.get("eq") or {}).get("gains") or [0.0] * len(p.EQ_BAND_FREQS))
            index = int(rest[1])
            if not 0 <= index < len(gains):
                raise CommandError("band index out of range")
            gains[index] = float(rest[2])
            return ("eq-custom", gains)
        raise CommandError("eq preset|custom|band")
    if cmd == "voice-prompts":
        need(1)
        return ("voice-prompts", _bool(rest[0]))
    if cmd == "voice-aware":
        need(1)
        return ("voice-aware", int(rest[0]))
    if cmd == "find":
        need(1)
        side = rest[0].lower()
        table = {"left": (True, None), "right": (None, True), "both": (True, True), "stop": (False, False)}
        if side not in table:
            raise CommandError("find left|right|both|stop")
        return ("find",) + table[side]
    if cmd == "gesture":
        need(2)
        if rest[0] not in p.GESTURES or rest[1] not in p.GESTURE_ACTIONS:
            raise CommandError("unknown gesture or action")
        return ("gesture", rest[0], rest[1])
    if cmd == "auto-off":
        need(1)
        return ("auto-off", _bool(rest[0]), int(rest[1]) if len(rest) > 1 else None)
    if cmd == "spatial":
        need(1)
        scene = rest[1] if len(rest) > 1 else None
        if scene and scene not in p.SPATIAL_SCENES.values():
            raise CommandError("spatial scene movie|music|game")
        return ("spatial", _bool(rest[0]), scene)
    if cmd == "safe-sound":
        need(1)
        return ("safe-sound", _bool(rest[0]))
    if cmd == "raw":
        need(1)
        try:
            return ("raw", p.raw_frame(bytes.fromhex("".join(rest))))
        except ValueError as e:
            raise CommandError(str(e)) from None
    if cmd == "low-latency":
        need(1)
        return ("low-latency", _bool(rest[0]))
    raise CommandError("unknown command %r" % cmd)
