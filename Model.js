.pragma library

// View logic for the JBL Control widget. Pure functions over the state the
// bridge prints (one JSON object per line), so the node tests and the QML
// share one implementation.

var MODE_LABELS = { off: "Off", anc: "Noise Cancelling", ambient: "Ambient Aware", talkthru: "TalkThru" }
var MODE_SHORT = { off: "Off", anc: "ANC", ambient: "Ambient", talkthru: "TalkThru" }
// Nerd Font (Material Design) glyphs, by codepoint.
var GLYPHS = {
  earbuds: String.fromCodePoint(0xF184F),      // md-earbuds
  earbudsOff: String.fromCodePoint(0xF1850),   // md-earbuds_off
  headphones: String.fromCodePoint(0xF02CB),   // md-headphones
  headphonesOff: String.fromCodePoint(0xF07CE) // md-headphones_off
}
var MODE_GLYPHS = {
  off: String.fromCodePoint(0xF07CE),      // md-headphones_off
  anc: String.fromCodePoint(0xF02CB),      // md-headphones
  ambient: String.fromCodePoint(0xF07C5),  // md-ear_hearing
  talkthru: String.fromCodePoint(0xF05CB)  // md-account_voice
}

// Bar icon: earbuds for TWS (left/right reported), headphones otherwise.
function deviceGlyph(state) {
  var b = (state && state.battery) || {}
  var tws = b.hasOwnProperty("left") || b.hasOwnProperty("right")
  if (!state || !state.connected) return tws ? GLYPHS.earbudsOff : GLYPHS.headphonesOff
  return tws || !b.main ? GLYPHS.earbuds : GLYPHS.headphones
}
var MODE_ORDER = ["anc", "ambient", "talkthru", "off"]

var EQ_PRESETS = [
  { value: "off", label: "Off" },
  { value: "1", label: "Jazz" },
  { value: "2", label: "Vocal" },
  { value: "3", label: "Bass" },
  { value: "6", label: "Club" },
  { value: "7", label: "Studio" },
  { value: "9", label: "Extreme Bass" },
  { value: "custom", label: "Custom" }
]
var EQ_BANDS = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

function parseLine(line) {
  try {
    var msg = JSON.parse(line)
    return msg && typeof msg === "object" ? msg : null
  } catch (e) {
    return null
  }
}

function features(state) {
  return (state && state.features) || {}
}

function ready(state) {
  return !!(state && state.connected && state.control === "ready")
}

// Battery entries to show, in display order: [{key, label, level, charging}].
function batteryRows(state) {
  var b = (state && state.battery) || {}
  var rows = []
  if (b.main) rows.push(row("main", "Battery", b.main))
  var parts = [["left", "Left"], ["right", "Right"], ["case", "Case"]]
  for (var i = 0; i < parts.length; i++) {
    var key = parts[i][0]
    if (b.hasOwnProperty(key)) rows.push(row(key, parts[i][1], b[key]))
  }
  if (rows.length === 0 && state && typeof state.bluezBattery === "number")
    rows.push({ key: "main", label: "Battery", level: state.bluezBattery, charging: false })
  return rows
}

function row(key, label, v) {
  return { key: key, label: label, level: v ? v.level : null, charging: !!(v && v.charging) }
}

// The number for the bar: the emptier bud (that is the one that runs out),
// ignoring the case and anything not reporting.
function barLevel(state) {
  var rows = batteryRows(state)
  var min = null
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].key === "case" || rows[i].level === null) continue
    if (min === null || rows[i].level < min) min = rows[i].level
  }
  return min
}

function charging(state) {
  var rows = batteryRows(state)
  for (var i = 0; i < rows.length; i++)
    if (rows[i].key !== "case" && rows[i].charging) return true
  return false
}

function barText(state, showPercent) {
  if (!state || !state.connected) return ""
  var level = barLevel(state)
  return showPercent && level !== null ? level + "%" : ""
}

function tooltip(state) {
  if (!state || !state.connected) return "JBL: no headphones connected"
  var parts = []
  var rows = batteryRows(state)
  for (var i = 0; i < rows.length; i++)
    if (rows[i].level !== null) parts.push(rows[i].label + " " + rows[i].level + "%" + (rows[i].charging ? " ⚡" : ""))
  var head = state.name || "JBL"
  if (state.mode && MODE_LABELS[state.mode]) head += " · " + MODE_LABELS[state.mode]
  return parts.length ? head + "\n" + parts.join("  ") : head
}

function modeOptions(state) {
  var modes = features(state).modes || []
  var out = []
  for (var i = 0; i < MODE_ORDER.length; i++) {
    var m = MODE_ORDER[i]
    if (modes.indexOf(m) >= 0) out.push({ value: m, label: MODE_SHORT[m], tooltip: MODE_LABELS[m] })
  }
  return out
}

// Earbuds with one bud out (in the case) refuse ANC on JBL firmware. Returns
// the hint to show under the mode buttons, or "".
function modeHint(state) {
  var b = (state && state.battery) || {}
  var modes = features(state).modes || []
  if (modes.indexOf("anc") < 0 || !b.hasOwnProperty("left") || !b.hasOwnProperty("right")) return ""
  var left = b.left && b.left.level !== null && b.left.level !== undefined
  var right = b.right && b.right.level !== null && b.right.level !== undefined
  if (left === right) return ""
  return (left ? "Right" : "Left") + " bud is not connected. Noise Cancelling needs both buds in your ears."
}

function modeGlyph(state) {
  return (state && MODE_GLYPHS[state.mode]) || MODE_GLYPHS.anc
}

// Modes the bar's scroll wheel / keybinding cycles through, filtered to what
// the device has. `wanted` is the cycleModes setting (array or comma list).
function cycleList(state, wanted) {
  var modes = features(state).modes || []
  if (typeof wanted === "string") wanted = wanted.split(",")
  if (!wanted || !wanted.length) wanted = MODE_ORDER
  var out = []
  for (var i = 0; i < wanted.length; i++) {
    var m = String(wanted[i]).trim()
    if (modes.indexOf(m) >= 0 && out.indexOf(m) < 0) out.push(m)
  }
  return out
}

function cycleCommand(state, wanted) {
  var list = cycleList(state, wanted)
  return list.length ? "mode cycle " + list.join(",") : ""
}

function eqValue(state) {
  var eq = state && state.eq
  if (!eq) return "off"
  return String(eq.preset)
}

function eqCommand(value) {
  if (value === "off") return "eq off"
  if (value === "custom") return ""
  return "eq preset " + value
}

function eqGains(state) {
  var g = state && state.eq && state.eq.gains
  var out = []
  for (var i = 0; i < EQ_BANDS.length; i++) out.push(g && typeof g[i] === "number" ? g[i] : 0)
  return out
}

function eqBandCommand(index, gain) {
  return "eq band " + index + " " + (Math.round(gain * 2) / 2)
}

function formatGain(gain) {
  var v = Math.round(gain * 2) / 2
  return (v > 0 ? "+" : "") + v
}

// True once when the emptier bud crosses `threshold` going down.
function lowBatteryCrossed(prevLevel, level, threshold) {
  if (!threshold || threshold <= 0 || level === null || level === undefined) return false
  if (prevLevel === null || prevLevel === undefined) return level <= threshold
  return prevLevel > threshold && level <= threshold
}

function controlNote(state) {
  if (!state || !state.connected) return ""
  switch (state.control) {
    case "ready": return ""
    case "waiting": return "Waiting for the buds' control channel…"
    case "connecting": return "Connecting to the controls…"
    case "reconnecting": return "Reconnecting to the controls…"
    case "unavailable": return "Controls not reachable right now. Battery still updates."
    case "powering-off": return "Powering off…"
    default: return ""
  }
}

function infoLine(state) {
  var parts = []
  if (state && state.firmware) parts.push("Firmware " + state.firmware)
  if (state && state.serial) parts.push(state.serial)
  return parts.join(" · ")
}

if (typeof module !== "undefined") {
  module.exports = {
    MODE_LABELS: MODE_LABELS, MODE_ORDER: MODE_ORDER, EQ_PRESETS: EQ_PRESETS, EQ_BANDS: EQ_BANDS,
    GLYPHS: GLYPHS, MODE_GLYPHS: MODE_GLYPHS, deviceGlyph: deviceGlyph,
    parseLine: parseLine, features: features, ready: ready, batteryRows: batteryRows,
    barLevel: barLevel, charging: charging, barText: barText, tooltip: tooltip,
    modeOptions: modeOptions, modeGlyph: modeGlyph, modeHint: modeHint, cycleList: cycleList, cycleCommand: cycleCommand,
    eqValue: eqValue, eqCommand: eqCommand, eqGains: eqGains, eqBandCommand: eqBandCommand,
    formatGain: formatGain, lowBatteryCrossed: lowBatteryCrossed, controlNote: controlNote,
    infoLine: infoLine
  }
}
