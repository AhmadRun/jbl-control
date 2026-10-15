import QtQuick
import qs.Commons

// One battery line in the panel: label, level bar, percentage. A component
// that is not reporting (a bud in the case, say) keeps its row with a dash.
Item {
  id: row

  property color foreground: Color.foreground
  property string fontFamily: Style.font.family
  property string label: ""
  property var level: null
  property bool charging: false

  readonly property bool known: level !== null && level !== undefined
  readonly property bool low: known && level <= 15 && !charging

  implicitHeight: Style.space(22)

  Text {
    id: name
    textFormat: Text.PlainText
    text: row.label
    color: row.foreground
    font.family: row.fontFamily
    font.pixelSize: Style.font.body
    width: Style.space(56)
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
  }

  Text {
    id: value
    textFormat: Text.PlainText
    // nf-md-lightning_bolt marks charging.
    text: !row.known ? "—" : row.level + "%" + (row.charging ? " " + String.fromCodePoint(0xF140B) : "")
    color: row.low ? Color.urgent : row.foreground
    font.family: row.fontFamily
    font.pixelSize: Style.font.body
    horizontalAlignment: Text.AlignRight
    width: Style.space(64)
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
  }

  Rectangle {
    id: track
    anchors.left: name.right
    anchors.leftMargin: Style.space(8)
    anchors.right: value.left
    anchors.rightMargin: Style.space(8)
    anchors.verticalCenter: parent.verticalCenter
    height: Style.space(6)
    radius: height / 2
    color: Qt.rgba(row.foreground.r, row.foreground.g, row.foreground.b, 0.12)

    Rectangle {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      height: parent.height
      radius: parent.radius
      color: row.low ? Color.urgent : row.foreground
      opacity: row.known ? 1 : 0
      width: row.known ? Math.max(parent.height, parent.width * Math.max(0, Math.min(1, row.level / 100))) : 0

      Behavior on width { NumberAnimation { duration: 280; easing.type: Easing.OutCubic } }
    }
  }
}
