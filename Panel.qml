import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.commons
import qs.ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "evo.trading"
  ipcTarget: "evo.trading"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: Theme.foreground
  readonly property color urgent: bar ? bar.urgent : Theme.urgent
  readonly property color accent: Theme.accent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color surface: Theme.popups.background
  readonly property string fontFamily: bar ? bar.fontFamily : Theme.font.family

  readonly property string statusScript: Qt.resolvedUrl("bin/trading-status").toString().replace("file://", "")
  readonly property int refreshIntervalSec: Math.max(30, parseInt(setting("refreshIntervalSec", 300), 10) || 300)

  property bool loading: true
  property var data: Model.emptyData("")

  readonly property bool hasData: data.ok === true
  readonly property bool iconError: !loading && !hasData && data.error !== ""
  readonly property bool iconBusy: loading
  readonly property bool iconMuted: false
  readonly property string barTooltip: Model.barTooltip(data, loading)
  readonly property string deskIcon: Model.deskIcon()
  readonly property string barAmount: Model.barAmount(data)
  readonly property string barIcon: barAmount !== "" ? deskIcon : ""
  readonly property string barValue: Model.barValue(data)
  readonly property var allPositions: hasData && data.positions instanceof Array ? data.positions : []
  readonly property var positions: allPositions.length > 12 ? allPositions.slice(0, 12) : allPositions
  readonly property int positionHidden: Math.max(0, allPositions.length - positions.length)
  readonly property var unrealized: Model.unrealizedUsd(allPositions)
  readonly property string badge: Model.deskBadge(data)
  readonly property real pairColWidth: columnWidth(pairMetrics, positions, function(p) {
    return Model.plain(p && (p.wsname || p.pair_id))
  })
  readonly property real lastColWidth: columnWidth(detailMetrics, positions, function(p) {
    return Model.fmtUsd(p && p.last)
  })
  readonly property real entryColWidth: columnWidth(detailMetrics, positions, function(p) {
    return Model.fmtUsd(p && p.entry)
  })
  readonly property real pctColWidth: columnWidth(detailMetrics, positions, function(p) {
    return Model.fmtPct(p && p.pct_vs_entry)
  })
  readonly property color tone: {
    if (iconError || (hasData && data.halted)) return urgent
    var n = parseFloat(unrealized)
    if (isNaN(n) || n === 0) return foreground
    return n > 0 ? accent : urgent
  }

  function columnWidth(metrics, list, pick) {
    var max = 0
    var rows = list || []
    for (var i = 0; i < rows.length; i++)
      max = Math.max(max, metrics.advanceWidth(pick(rows[i])))
    return Math.ceil(max)
  }

  function pnlColor(val) {
    var n = parseFloat(val)
    if (isNaN(n) || n === 0) return foreground
    return n > 0 ? accent : urgent
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function applyPayload(raw) {
    loading = false
    data = Model.parsePayload(raw)
  }

  function refresh() {
    if (!statusScript || statusProc.running) return
    loading = true
    statusProc.command = ["bash", statusScript]
    statusProc.running = true
  }

  function openDesk() {
    var rootPath = hasData && data.desk_root ? String(data.desk_root) : ""
    if (rootPath.length < 2 || rootPath.charAt(0) !== "/" || rootPath.indexOf("\n") >= 0 || rootPath.indexOf("=") >= 0)
      return
    var terminal = String(Quickshell.env("EVOSHELL_TERMINAL") || "ghostty")
    Quickshell.execDetached([
      terminal,
      "--working-directory=" + rootPath,
      "-e",
      "cursor-agent"
    ])
    root.close()
  }

  function openFromHotkey() {
    root.controller.show()
    root.refresh()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.openFromHotkey()
  }


  FontMetrics {
    id: pairMetrics
    font.family: root.fontFamily
    font.pixelSize: Theme.font.body
    font.bold: true
  }

  FontMetrics {
    id: detailMetrics
    font.family: root.fontFamily
    font.pixelSize: Theme.font.caption
  }

  Component.onCompleted: refresh()

  onOpenedChanged: if (opened) {
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProc
    property string stdoutBuf: ""
    onStarted: stdoutBuf = ""
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        statusProc.stdoutBuf += chunk
        if (statusProc.stdoutBuf.length > 131072) {
          statusProc.signal(15)
          statusProc.stdoutBuf = ""
        }
      }
    }
    onExited: function() {
      root.applyPayload(String(stdoutBuf || "").trim())
    }
  }

  IpcHandler {
    enabled: !!root.hostWidget && root.hostWidget.ownsIpc
    target: root.ipcTarget
    function open(): void { root.openFromHotkey() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Theme.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Theme.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Theme.space(12)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.loading && !root.hasData
            text: "Refreshing desk…"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Theme.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.iconError
            text: Model.plain(root.data.error)
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Theme.font.body
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
          }

          PanelHero {
            width: parent.width
            visible: root.hasData
            title: Model.plain(Model.fmtUsd(root.data.equity_usd))
            meta: Model.plain(Model.heroMeta(root.data))
            detail: Model.plain(root.badge)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.loading ? 0.55 : 1

            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.deskIcon
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Theme.font.display
                opacity: 0.92
              }
            }
          }

          Rectangle {
            visible: root.hasData && root.data.halted
            width: parent.width
            implicitHeight: haltText.implicitHeight + Theme.space(12)
            radius: Theme.cornerRadius
            color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.14)
            border.width: 1
            border.color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.4)

            Text {
              id: haltText
              textFormat: Text.PlainText
              anchors.centerIn: parent
              text: "Halted — new fills are stopped"
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Theme.font.bodySmall
              font.bold: true
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.hasData && !!root.data.balance_error
            text: Model.plain(root.data.balance_error, 180)
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
            wrapMode: Text.WordWrap
          }

          Row {
            visible: root.hasData
            width: parent.width
            spacing: Theme.space(16)

            StatTile {
              width: (parent.width - parent.spacing * 3) / 4
              value: Model.fmtUsd(root.data.usd_spot)
              label: "cash"
            }
            StatTile {
              width: (parent.width - parent.spacing * 3) / 4
              value: Model.fmtUsd(root.data.allocated_usd)
              label: "allocated"
            }
            StatTile {
              width: (parent.width - parent.spacing * 3) / 4
              value: Model.fmtSignedUsd(root.unrealized)
              label: "open pnl"
              valueColor: root.pnlColor(root.unrealized)
            }
            StatTile {
              width: (parent.width - parent.spacing * 3) / 4
              value: Model.fmtUsd(root.data.halt_floor_usd)
              label: "halt floor"
              valueColor: root.dim
            }
          }

          PanelSeparator {
            visible: root.hasData
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.hasData
            width: parent.width
            text: "POSITIONS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.hasData && root.positions.length === 0
            text: "Nothing open"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.bodySmall
            horizontalAlignment: Text.AlignHCenter
          }

          Repeater {
            model: root.positions
            delegate: PositionRow {
              required property var modelData
              width: column.width
              pos: modelData
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.positionHidden > 0
            text: "+" + root.positionHidden + " more"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

        }
      }
    }
  }

  component StatTile: Item {
    id: tile
    property string value: ""
    property string label: ""
    property color valueColor: root.foreground

    implicitWidth: Theme.space(108)
    implicitHeight: Theme.font.heading + Theme.space(56)

    Rectangle {
      id: frame
      anchors.fill: parent
      anchors.topMargin: legendChip.visible ? legendChip.height / 2 : 0
      color: "transparent"
      radius: Theme.space(8)
      border.width: 1
      border.color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.9)
      antialiasing: true
    }

    Item {
      id: legendChip
      x: Theme.space(14)
      y: 0
      width: legendTextItem.implicitWidth + Theme.space(8)
      height: Math.max(1, legendTextItem.implicitHeight)
      visible: tile.label !== ""

      Rectangle {
        anchors.fill: parent
        color: Theme.popups.background
      }

      Text {
        id: legendTextItem
        x: Theme.space(4)
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: tile.label
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Theme.font.caption
        font.bold: true
      }
    }

    Text {
      anchors.fill: frame
      anchors.leftMargin: Theme.space(6)
      anchors.rightMargin: Theme.space(6)
      textFormat: Text.PlainText
      text: tile.value
      color: tile.valueColor
      font.family: root.fontFamily
      font.pixelSize: Theme.font.display
      font.bold: true
      horizontalAlignment: Text.AlignHCenter
      verticalAlignment: Text.AlignVCenter
      elide: Text.ElideRight
      fontSizeMode: Text.HorizontalFit
      minimumPixelSize: Theme.font.caption
    }
  }

  component PositionRow: BorderSurface {
    id: row
    property var pos: ({})

    implicitHeight: posLine.implicitHeight + Theme.space(16)
    color: Theme.popups.background
    borderSpec: Border.surfaceSpec("popups", "border", Theme.popups.border, 1)
    radius: Theme.cornerRadius
    clip: true

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: 3
      color: root.pnlColor(row.pos && row.pos.pnl_usd)
    }

    Item {
      id: posLine
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Theme.space(12)
      anchors.rightMargin: Theme.space(10)
      implicitHeight: Math.max(pairText.implicitHeight, detailRow.implicitHeight, pnlText.implicitHeight)

      Text {
        id: pairText
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: root.pairColWidth
        textFormat: Text.PlainText
        text: Model.plain(row.pos && (row.pos.wsname || row.pos.pair_id))
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Theme.font.body
        font.bold: true
        elide: Text.ElideRight
      }

      Row {
        id: detailRow
        anchors.left: pairText.right
        anchors.leftMargin: Theme.space(10)
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.space(8)

        Row {
          spacing: Theme.space(4)
          Text {
            textFormat: Text.PlainText
            text: "Last"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
          }
          Text {
            width: root.lastColWidth
            horizontalAlignment: Text.AlignRight
            textFormat: Text.PlainText
            text: Model.plain(Model.fmtUsd(row.pos && row.pos.last))
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
          }
        }

        Text {
          textFormat: Text.PlainText
          text: "·"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Theme.font.caption
        }

        Row {
          spacing: Theme.space(4)
          Text {
            textFormat: Text.PlainText
            text: "entry"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
          }
          Text {
            width: root.entryColWidth
            horizontalAlignment: Text.AlignRight
            textFormat: Text.PlainText
            text: Model.plain(Model.fmtUsd(row.pos && row.pos.entry))
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Theme.font.caption
          }
        }

        Text {
          textFormat: Text.PlainText
          text: "·"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Theme.font.caption
        }

        Text {
          width: root.pctColWidth
          horizontalAlignment: Text.AlignRight
          textFormat: Text.PlainText
          text: Model.plain(Model.fmtPct(row.pos && row.pos.pct_vs_entry))
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Theme.font.caption
        }
      }

      Text {
        id: pnlText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: Model.plain(Model.fmtSignedUsd(row.pos && row.pos.pnl_usd))
        color: root.pnlColor(row.pos && row.pos.pnl_usd)
        font.family: root.fontFamily
        font.pixelSize: Theme.font.body
        font.bold: true
      }
    }
  }
}
