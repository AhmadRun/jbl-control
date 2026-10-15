// Run with: node tests/test_model.js
const fs = require("fs")
const path = require("path")
const vm = require("vm")
const assert = require("assert")

const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
  .replace(/^\.pragma library\s*$/m, "")
const sandbox = { module: { exports: {} } }
vm.runInNewContext(source, sandbox)
const M = sandbox.module.exports

// The bridge's state for a JBL Wave Buds 2 (right bud in the case).
const waveBuds2 = {
  type: "state", connected: true, control: "ready", name: "JBL Wave Buds 2",
  address: "50:1B:6A:2F:2D:FF", model: "ea59a0", mode: "ambient", firmware: "2.7.0",
  serial: "DO0032-JO0230678",
  battery: { left: { level: 90, charging: false }, right: null, case: { level: 100, charging: false } },
  eq: { preset: "off", gains: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0] },
  features: { modes: ["off", "anc", "ambient", "talkthru"], eq: true, voicePrompts: true,
              voiceAware: true, findBuds: true, lowLatency: false, autoOff: true, safeSound: true,
              spatial: false, ancLevel: false, powerOff: true }
}

let passed = 0
// Arrays made inside the vm sandbox have their own Array prototype; compare by value.
function same(a, b) { assert.strictEqual(JSON.stringify(a), JSON.stringify(b)) }
function test(name, fn) {
  try {
    fn()
    passed++
  } catch (e) {
    console.error("FAIL " + name + "\n  " + e.message)
    process.exitCode = 1
  }
}

test("parseLine ignores junk", () => {
  assert.strictEqual(M.parseLine("not json"), null)
  assert.strictEqual(M.parseLine('{"type":"state"}').type, "state")
})

test("battery rows keep a missing bud as a row without a level", () => {
  const rows = M.batteryRows(waveBuds2)
  same(rows.map(r => r.key), ["left", "right", "case"])
  assert.strictEqual(rows[1].level, null)
})

test("bar level is the emptier reporting bud, never the case", () => {
  assert.strictEqual(M.barLevel(waveBuds2), 90)
  const both = Object.assign({}, waveBuds2, { battery: {
    left: { level: 40 }, right: { level: 70 }, case: { level: 10 } } })
  assert.strictEqual(M.barLevel(both), 40)
  assert.strictEqual(M.barText(both, true), "40%")
  assert.strictEqual(M.barText(both, false), "")
})

test("headphones: single battery and BlueZ fallback", () => {
  assert.strictEqual(M.barLevel({ connected: true, battery: { main: { level: 55 } } }), 55)
  assert.strictEqual(M.barLevel({ connected: true, battery: {}, bluezBattery: 70 }), 70)
  assert.strictEqual(M.deviceGlyph({ connected: true, battery: { main: { level: 55 } } }), M.GLYPHS.headphones)
  assert.strictEqual(M.deviceGlyph(waveBuds2), M.GLYPHS.earbuds)
  assert.strictEqual(M.deviceGlyph({ connected: false, battery: {} }), M.GLYPHS.headphonesOff)
})

test("disconnected", () => {
  const off = { type: "state", connected: false, features: {}, battery: {} }
  assert.strictEqual(M.barText(off, true), "")
  assert.ok(M.tooltip(off).includes("no headphones"))
  assert.strictEqual(M.ready(off), false)
})

test("tooltip lists mode and battery", () => {
  const t = M.tooltip(waveBuds2)
  assert.ok(t.startsWith("JBL Wave Buds 2 · Ambient Aware"))
  assert.ok(t.includes("Left 90%") && t.includes("Case 100%") && !t.includes("Right"))
})

test("mode options follow the device, in a fixed order", () => {
  same(M.modeOptions(waveBuds2).map(o => o.value), ["anc", "ambient", "talkthru", "off"])
  const noAnc = Object.assign({}, waveBuds2, { features: { modes: ["off", "ambient", "talkthru"] } })
  same(M.modeOptions(noAnc).map(o => o.value), ["ambient", "talkthru", "off"])
})

test("cycle list honours the setting and the device", () => {
  assert.strictEqual(M.cycleCommand(waveBuds2, ["anc", "ambient"]), "mode cycle anc,ambient")
  assert.strictEqual(M.cycleCommand(waveBuds2, "ambient, off, bogus"), "mode cycle ambient,off")
  assert.strictEqual(M.cycleCommand(waveBuds2, []), "mode cycle anc,ambient,talkthru,off")
  assert.strictEqual(M.cycleCommand({ features: {} }, ["anc"]), "")
})

test("eq helpers", () => {
  assert.strictEqual(M.eqValue(waveBuds2), "off")
  assert.strictEqual(M.eqValue({ eq: { preset: 3 } }), "3")
  assert.strictEqual(M.eqCommand("3"), "eq preset 3")
  assert.strictEqual(M.eqCommand("off"), "eq off")
  assert.strictEqual(M.eqCommand("custom"), "")
  assert.strictEqual(M.eqBandCommand(2, 3.3), "eq band 2 3.5")
  assert.strictEqual(M.formatGain(-1.24), "-1")
  assert.strictEqual(M.formatGain(2), "+2")
  assert.strictEqual(M.eqGains({}).length, 10)
})

test("low battery fires once on the way down", () => {
  assert.strictEqual(M.lowBatteryCrossed(30, 20, 20), true)
  assert.strictEqual(M.lowBatteryCrossed(20, 15, 20), false)
  assert.strictEqual(M.lowBatteryCrossed(null, 10, 20), true)
  assert.strictEqual(M.lowBatteryCrossed(null, 50, 20), false)
  assert.strictEqual(M.lowBatteryCrossed(30, 10, 0), false)
})

test("ANC hint when one bud is out", () => {
  assert.ok(M.modeHint(waveBuds2).startsWith("Right bud is not connected"))
  const both = Object.assign({}, waveBuds2, { battery: { left: { level: 50 }, right: { level: 60 } } })
  assert.strictEqual(M.modeHint(both), "")
  assert.strictEqual(M.modeHint({ connected: true, battery: { main: { level: 50 } }, features: { modes: ["off", "anc"] } }), "")
})

test("control notes", () => {
  assert.strictEqual(M.controlNote(waveBuds2), "")
  assert.ok(M.controlNote(Object.assign({}, waveBuds2, { control: "unavailable" })).includes("Battery still"))
  assert.strictEqual(M.infoLine(waveBuds2), "Firmware 2.7.0 · DO0032-JO0230678")
})

console.log(passed + " model tests passed" + (process.exitCode ? " (with failures above)" : ""))
