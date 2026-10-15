import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

// JBL earbuds/headphones in the bar: battery, listening mode, EQ and the rest
// of the JBL Headphones app's settings. All Bluetooth work happens in
// bin/jbl-bridge; this widget prints commands to it and renders the state it
// prints back (one JSON object per line).
Panel {
  id: root
  moduleName: "io.github.ahmadrun.jbl-control"
  ipcTarget: "io.github.ahmadrun.jbl-control"
  // Own IpcHandler below, for the extra methods (cycleAnc, setMode, ...).
  manageIpc: false

  // ---- Settings (shell.json via the plugin settings form)
  readonly property string address: String(setting("address", "")).trim()
  readonly property bool showPercent: setting("showPercent", true) !== false
  readonly property var cycleModes: setting("cycleModes", ["anc", "ambient", "off"])
  readonly property bool osdEnabled: setting("osd", true) !== false
  readonly property int lowBattery: Math.max(0, Math.min(50, Number(setting("lowBattery", 15)) || 0))

  // ---- Bridge state
  property var jbl: ({ connected: false, features: {}, battery: {} })
  readonly property var features: Model.features(jbl)
  readonly property bool ready: Model.ready(jbl)
  property var lastLevel: null
  property string flashText: ""
  property bool customEq: false

  readonly property color fg: root.bar ? root.bar.foreground : Color.foreground
  readonly property string ff: root.bar ? root.bar.fontFamily : Style.font.family

  readonly property string bridgePath: {
    var url = String(Qt.resolvedUrl("bin/jbl-bridge"))
    return url.indexOf("file://") === 0 ? decodeURIComponent(url.substring(7)) : url
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function send(line) {
    if (!line || !bridge.running) return
    bridge.write(line + "\n")
  }

  function setMode(mode) { send("mode " + mode) }

  function cycleMode() {
    var cmd = Model.cycleCommand(jbl, cycleModes)
    if (cmd) send(cmd)
  }

  function flash(text) {
    flashText = text || ""
    flashTimer.restart()
    // Feedback for keybinding / scroll use, where the panel is not open.
    if (text && !root.opened && root.osdEnabled && jbl.primary) showOsd(Model.modeGlyph(jbl), text)
  }

  function showOsd(icon, message) {
    if (!bar || !bar.shell) return
    bar.shell.summon("omarchy.osd", JSON.stringify({ icon: icon, message: message }))
  }

  function onBridgeLine(line) {
    var msg = Model.parseLine(line)
    if (!msg) return
    if (msg.type === "error") {
      flash(msg.message)
      return
    }
    if (msg.type !== "state") return
    var prev = jbl
    jbl = msg
    if (!msg.primary) return
    if (osdEnabled && prev.connected && msg.connected && prev.mode && msg.mode && prev.mode !== msg.mode)
      showOsd(Model.modeGlyph(msg), Model.MODE_LABELS[msg.mode] || msg.mode)
    if (msg.errorAt && msg.errorAt !== prev.errorAt) flash(msg.lastError)
    if (!msg.connected) {
      lastLevel = null
      return
    }
    var level = Model.barLevel(msg)
    if (level !== null) {
      if (Model.lowBatteryCrossed(lastLevel, level, lowBattery) && !Model.charging(msg))
        Quickshell.execDetached(["notify-send", "-a", "JBL Control", "-i", "audio-headphones",
                                 (msg.name || "JBL") + " battery low", level + "% left"])
      lastLevel = level
    }
  }

  Process {
    id: bridge
    command: root.address !== "" ? [root.bridgePath, "--address", root.address] : [root.bridgePath]
    running: true
    stdinEnabled: true
    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(line) { root.onBridgeLine(line) }
    }
    // The bridge only exits when something went badly wrong; try again later.
    onExited: restartTimer.restart()
  }

  Timer {
    id: restartTimer
    interval: 5000
    onTriggered: bridge.running = true
  }

  Timer {
    id: flashTimer
    interval: 6000
    onTriggered: root.flashText = ""
  }

  // Scroll on the bar icon: one step per notch, not one per pixel of a touchpad.
  Timer {
    id: wheelGate
    interval: 350
  }

  IpcHandler {
    target: "io.github.ahmadrun.jbl-control"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function cycleAnc(): void { root.cycleMode() }
    function setMode(mode: string): void { root.setMode(mode) }
    function findBuds(side: string): void { root.send("find " + (side || "both")) }
    function command(line: string): void { root.send(line) }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: {
      var glyph = Model.deviceGlyph(root.jbl)
      var pct = Model.barText(root.jbl, root.showPercent)
      return pct ? glyph + " " + pct : glyph
    }
    dimmed: !root.jbl.connected
    active: root.jbl.connected && Model.barLevel(root.jbl) !== null && Model.barLevel(root.jbl) <= root.lowBattery
      && !Model.charging(root.jbl)
    horizontalMargin: 8.75
    fixedWidth: root.bar && root.bar.vertical ? Style.bar.statusSlot : -1
    tooltipText: Model.tooltip(root.jbl)
    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.cycleMode()
      else root.toggle()
    }
    onWheelMoved: function(delta) {
      if (wheelGate.running || !root.ready) return
      wheelGate.start()
      root.cycleMode()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "m" || t === "M") root.cycleMode()
      }

      ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: column.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Column {
          id: column
          width: scroll.availableWidth
          spacing: Style.space(12)

          // ---------- Hero ----------
          PanelHero {
            foreground: root.fg
            fontFamily: root.ff
            title: root.jbl.connected ? (root.jbl.name || "JBL") : "JBL Control"
            meta: {
              if (!root.jbl.connected) return "No JBL headphones connected"
              var note = Model.controlNote(root.jbl)
              if (note) return note
              return Model.MODE_LABELS[root.jbl.mode] || ""
            }
            detail: root.flashText
            iconOpacity: root.jbl.connected ? 1 : 0.5
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: Model.deviceGlyph(root.jbl)
                color: root.fg
                font.family: root.ff
                font.pixelSize: Style.font.display
              }
            }
          }

          // ---------- Battery ----------
          Column {
            width: parent.width
            spacing: Style.space(6)
            visible: root.jbl.connected && batteryRepeater.count > 0

            Repeater {
              id: batteryRepeater
              model: Model.batteryRows(root.jbl)

              delegate: BatteryRow {
                required property var modelData
                width: column.width
                foreground: root.fg
                fontFamily: root.ff
                label: modelData.label
                level: modelData.level
                charging: modelData.charging
              }
            }
          }

          // ---------- Noise control ----------
          PanelSeparator { foreground: root.fg; visible: modeSection.visible }

          Column {
            id: modeSection
            width: parent.width
            spacing: Style.space(10)
            visible: root.ready && (root.features.modes || []).length > 1

            PanelSectionHeader { text: "NOISE CONTROL"; foreground: root.fg; fontFamily: root.ff }

            ButtonGroup {
              anchors.horizontalCenter: parent.horizontalCenter
              options: Model.modeOptions(root.jbl)
              value: root.jbl.mode || ""
              foreground: root.fg
              fontFamily: root.ff
              focusable: false
              onChanged: function(v) { root.setMode(v) }
            }

            Item {
              width: parent.width
              implicitHeight: Style.space(26)
              visible: !!root.features.ancLevel && root.jbl.mode === "anc"

              Text {
                id: ancLabel
                textFormat: Text.PlainText
                text: "ANC level"
                color: root.fg
                font.family: root.ff
                font.pixelSize: Style.font.body
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              PanelSlider {
                bar: root.bar
                anchors.left: ancLabel.right
                anchors.leftMargin: Style.space(12)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                height: parent.height
                minimum: 1
                maximum: 7
                step: 1
                integer: true
                tickCount: 7
                value: root.jbl.ancLevel || 1
                onReleased: function(v) { root.send("anc-level " + Math.round(v)) }
              }
            }
          }

          // ---------- Equalizer ----------
          PanelSeparator { foreground: root.fg; visible: eqSection.visible }

          Column {
            id: eqSection
            width: parent.width
            spacing: Style.space(10)
            visible: root.ready && !!root.features.eq

            PanelSectionHeader { text: "EQUALIZER"; foreground: root.fg; fontFamily: root.ff }

            Item {
              width: parent.width
              implicitHeight: eqPreset.implicitHeight

              Dropdown {
                id: eqPreset
                anchors.left: parent.left
                anchors.right: customButton.left
                anchors.rightMargin: Style.space(8)
                showLabel: false
                options: Model.EQ_PRESETS
                value: Model.eqValue(root.jbl)
                onChanged: function(v) {
                  if (v === "custom") root.customEq = true
                  else root.send(Model.eqCommand(v))
                }
              }

              PanelActionButton {
                id: customButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                // nf-md-tune_vertical
                iconText: String.fromCodePoint(0xF066A)
                tooltipText: root.customEq ? "Hide band sliders" : "Edit the 10-band curve"
                foreground: root.fg
                bordered: root.customEq
                onClicked: root.customEq = !root.customEq
              }
            }

            EqEditor {
              width: parent.width
              visible: root.customEq
              bar: root.bar
              foreground: root.fg
              fontFamily: root.ff
              gains: Model.eqGains(root.jbl)
              onBandReleased: function(index, gain) { root.send(Model.eqBandCommand(index, gain)) }
            }
          }

          // ---------- Settings ----------
          PanelSeparator { foreground: root.fg; visible: settingsSection.visible }

          Column {
            id: settingsSection
            width: parent.width
            spacing: Style.space(8)
            // From the features, not the rows' own `visible`: a child reports
            // invisible while its parent is, so that would never turn on.
            visible: root.ready && (!!root.features.voiceAware || !!root.features.voicePrompts
                                    || !!root.features.safeSound || !!root.features.spatial
                                    || !!root.features.autoOff || !!root.features.lowLatency)

            PanelSectionHeader { text: "SETTINGS"; foreground: root.fg; fontFamily: root.ff }

            Item {
              id: voiceAwareRow
              width: parent.width
              implicitHeight: Style.space(26)
              visible: !!root.features.voiceAware

              Text {
                id: vaLabel
                textFormat: Text.PlainText
                text: "VoiceAware"
                color: root.fg
                font.family: root.ff
                font.pixelSize: Style.font.body
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: vaValue
                textFormat: Text.PlainText
                text: vaSlider.liveValue > 0 ? String(Math.round(vaSlider.liveValue)) : "Off"
                color: root.fg
                font.family: root.ff
                font.pixelSize: Style.font.caption
                horizontalAlignment: Text.AlignRight
                width: Style.space(28)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }

              // How much of your own voice you hear on calls: 0 off .. 5 most.
              PanelSlider {
                id: vaSlider
                bar: root.bar
                anchors.left: vaLabel.right
                anchors.leftMargin: Style.space(12)
                anchors.right: vaValue.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                height: parent.height
                minimum: 0
                maximum: 5
                step: 1
                integer: true
                tickCount: 6
                value: root.jbl.voiceAware || 0
                onReleased: function(v) { root.send("voice-aware " + Math.round(v)) }
              }
            }

            Toggle {
              id: promptsToggle
              width: parent.width
              visible: !!root.features.voicePrompts
              label: "Spoken voice prompts"
              description: "Announce modes and battery in words instead of tones."
              checked: !!root.jbl.voicePrompts
              foreground: root.fg
              fontFamily: root.ff
              onClicked: root.send("voice-prompts " + (checked ? "off" : "on"))
            }

            Toggle {
              id: safeToggle
              width: parent.width
              visible: !!root.features.safeSound
              label: "SafeSound volume limit"
              description: "Cap the maximum volume to protect your hearing."
              checked: !!root.jbl.safeSound
              foreground: root.fg
              fontFamily: root.ff
              onClicked: root.send("safe-sound " + (checked ? "off" : "on"))
            }

            Toggle {
              id: spatialToggle
              width: parent.width
              visible: !!root.features.spatial
              label: "Spatial sound"
              description: root.jbl.spatial ? "Scene: " + root.jbl.spatial.scene : ""
              checked: !!(root.jbl.spatial && root.jbl.spatial.enabled)
              foreground: root.fg
              fontFamily: root.ff
              onClicked: root.send("spatial " + (checked ? "off" : "on"))
            }

            Toggle {
              id: autoOffToggle
              width: parent.width
              visible: !!root.features.autoOff
              label: "Auto power off"
              description: root.jbl.autoOff && root.jbl.autoOff.enabled && root.jbl.autoOff.minutes
                ? "After " + root.jbl.autoOff.minutes + " minutes without audio."
                : "Turn the buds off when nothing plays for a while."
              checked: !!(root.jbl.autoOff && root.jbl.autoOff.enabled)
              foreground: root.fg
              fontFamily: root.ff
              onClicked: root.send(checked ? "auto-off off" : "auto-off on 30")
            }

            Toggle {
              id: latencyToggle
              width: parent.width
              visible: !!root.features.lowLatency
              label: "Low latency (video & games)"
              description: "The buds reconnect their audio to switch."
              checked: !!root.jbl.lowLatency
              foreground: root.fg
              fontFamily: root.ff
              onClicked: root.send("low-latency " + (checked ? "off" : "on"))
            }
          }

          // ---------- Actions ----------
          PanelSeparator { foreground: root.fg; visible: actions.visible }

          Item {
            id: actions
            width: parent.width
            implicitHeight: Math.max(findRow.implicitHeight, powerButton.implicitHeight)
            visible: root.ready && (!!root.features.findBuds || !!root.features.powerOff)

            Row {
              id: findRow
              spacing: Style.space(8)
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              visible: !!root.features.findBuds
              readonly property var finding: root.jbl.finding || ({})

              Text {
                textFormat: Text.PlainText
                text: "Find"
                color: root.fg
                font.family: root.ff
                font.pixelSize: Style.font.body
                anchors.verticalCenter: parent.verticalCenter
              }

              Button {
                text: findRow.finding.left ? "Stop L" : "Left"
                bordered: true
                selected: !!findRow.finding.left
                foreground: root.fg
                fontFamily: root.ff
                onClicked: root.send(findRow.finding.left ? "find stop" : "find left")
              }

              Button {
                text: findRow.finding.right ? "Stop R" : "Right"
                bordered: true
                selected: !!findRow.finding.right
                foreground: root.fg
                fontFamily: root.ff
                onClicked: root.send(findRow.finding.right ? "find stop" : "find right")
              }
            }

            PanelActionButton {
              id: powerButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: !!root.features.powerOff
              // nf-md-power
              iconText: String.fromCodePoint(0xF0425)
              tooltipText: powerConfirm.running ? "Click again to power off" : "Power off the headphones"
              foreground: powerConfirm.running ? Color.urgent : root.fg
              onClicked: {
                if (powerConfirm.running) {
                  powerConfirm.stop()
                  root.send("power-off")
                } else {
                  powerConfirm.start()
                }
              }

              Timer { id: powerConfirm; interval: 3000 }
            }
          }

          // ---------- Footer ----------
          Text {
            width: parent.width
            textFormat: Text.PlainText
            visible: text !== ""
            text: {
              var parts = []
              var info = Model.infoLine(root.jbl)
              if (info) parts.push(info)
              if ((root.jbl.devices || []).length > 1) parts.push((root.jbl.devices || []).length + " JBL devices connected")
              return parts.join(" · ")
            }
            color: Qt.darker(root.fg, 1.5)
            font.family: root.ff
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }
    }
  }
}
