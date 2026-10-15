import QtQuick
import qs.Commons
import qs.Ui
import "../Model.js" as Model

// Ten-band custom EQ, 32 Hz to 16 kHz, -6 to +6 dB. A band is written to the
// buds when its slider is released (not while dragging: every write is a
// full 116-byte curve and the buds want them paced).
Column {
  id: editor

  property QtObject bar: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property var gains: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

  signal bandReleased(int index, real gain)
  signal flatten()

  spacing: Style.space(2)

  Repeater {
    model: Model.EQ_BANDS

    delegate: Item {
      required property var modelData
      required property int index

      width: editor.width
      implicitHeight: Style.space(26)

      Text {
        id: freq
        textFormat: Text.PlainText
        text: modelData
        color: Qt.darker(editor.foreground, 1.3)
        font.family: editor.fontFamily
        font.pixelSize: Style.font.caption
        width: Style.space(34)
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: gainText
        textFormat: Text.PlainText
        text: Model.formatGain(slider.liveValue) + " dB"
        color: editor.foreground
        font.family: editor.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignRight
        width: Style.space(48)
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }

      PanelSlider {
        id: slider
        bar: editor.bar
        anchors.left: freq.right
        anchors.right: gainText.left
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        minimum: -6
        maximum: 6
        step: 0.5
        tickCount: 3
        value: editor.gains[index] || 0
        onReleased: function(v) { editor.bandReleased(index, v) }
        onRightClicked: editor.bandReleased(index, 0)
      }
    }
  }
}
