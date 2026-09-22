import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "evo.trading"
  ipcTarget: "evo.trading"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color surface: Color.popups.background
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string statusScript: Qt.resolvedUrl("bin/trading-status").toString().replace("file://", "")
  readonly property string cycleScript: Qt.resolvedUrl("bin/run-desk-cycle").toString().replace("file://", "")
  readonly property int refreshIntervalSec: Math.max(30, parseInt(setting("refreshIntervalSec", 60), 10) || 60)

  property bool loading: true
  property var data: Model.emptyData("")
  property bool cycleRunning: false

  readonly property bool hasData: data.ok === true
  readonly property bool iconError: !loading && !hasData && data.error !== ""
  readonly property bool iconBusy: loading
  readonly property bool iconMuted: false
  readonly property string barTooltip: Model.barTooltip(data, loading)
  readonly property string barValue: Model.barValue(data)
  readonly property var allPositions: hasData && data.positions instanceof Array ? data.positions : []
  readonly property var positions: allPositions.length > 12 ? allPositions.slice(0, 12) : allPositions
  readonly property int positionHidden: Math.max(0, allPositions.length - positions.length)
  readonly property var unrealized: Model.unrealizedUsd(allPositions)
  readonly property string badge: Model.deskBadge(data, cycleRunning)
  readonly property color tone: {
    if (iconError || (hasData && data.halted)) return urgent
    var n = parseFloat(unrealized)
    if (isNaN(n) || n === 0) return foreground
    return n > 0 ? accent : urgent
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

  function runCycleNow() {
    if (!cycleScript || cycleProc.running) return
    cycleRunning = true
    cycleProc.command = ["bash", cycleScript]
    cycleProc.running = true
  }

  function openDesk() {
    var rootPath = hasData && data.desk_root ? String(data.desk_root) : ""
    if (rootPath.length < 2 || rootPath.charAt(0) !== "/" || rootPath.indexOf("\n") >= 0 || rootPath.indexOf("=") >= 0)
      return
    // org.omarchy.terminal is the default centered floating popup.
    Quickshell.execDetached([
      "/usr/bin/setsid",
      "/usr/bin/uwsm-app",
      "--",
      "/usr/bin/xdg-terminal-exec",
      "--app-id=org.omarchy.terminal",
      "--dir=" + rootPath,
      "-e",
      "/usr/share/omarchy/bin/omarchy-agent",
      "--inline"
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


  Component.onCompleted: refresh()

  onOpenedChanged: if (opened) {
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    running: root.opened
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

  Process {
    id: cycleProc
    onStarted: { }
    onExited: function() {
      root.cycleRunning = false
      root.refresh()
    }
  }

  IpcHandler {
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
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

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
          spacing: Style.space(12)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.loading && !root.hasData
            text: "Refreshing desk…"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.iconError
            text: Model.plain(root.data.error)
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
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
                text: ""
                color: root.tone
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
                opacity: 0.92
              }
            }
          }

          Rectangle {
            visible: root.hasData && root.data.halted
            width: parent.width
            implicitHeight: haltText.implicitHeight + Style.space(12)
            radius: Style.cornerRadius
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
              font.pixelSize: Style.font.bodySmall
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
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Row {
            visible: root.hasData
            width: parent.width
            spacing: Style.space(8)

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
            font.pixelSize: Style.font.bodySmall
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
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

          Row {
            id: actions
            width: parent.width
            spacing: Style.space(8)

            readonly property int buttonCount: openDesk.visible ? 3 : 2

            ActionButton {
              width: (parent.width - parent.spacing * (actions.buttonCount - 1)) / actions.buttonCount
              label: "Refresh"
              onClicked: root.refresh()
            }
            ActionButton {
              width: (parent.width - parent.spacing * (actions.buttonCount - 1)) / actions.buttonCount
              label: root.cycleRunning ? "Running…" : "Run cycle"
              enabled: !root.cycleRunning
              onClicked: root.runCycleNow()
            }
            ActionButton {
              id: openDesk
              width: (parent.width - parent.spacing * (actions.buttonCount - 1)) / actions.buttonCount
              visible: root.hasData && !!root.data.desk_root
              label: "Open desk"
              onClicked: root.openDesk()
            }
          }
        }
      }
    }
  }

  component StatTile: BorderSurface {
    id: tile
    property string value: ""
    property string label: ""
    property color valueColor: root.foreground

    implicitHeight: tileColumn.implicitHeight + Style.spacing.lg * 2
    color: Color.popups.background
    borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, 1)
    radius: Style.cornerRadius

    Column {
      id: tileColumn
      anchors.centerIn: parent
      width: parent.width - Style.spacing.lg * 2
      spacing: Style.spacing.labelGap

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: tile.value
        color: tile.valueColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: tile.label
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        elide: Text.ElideRight
      }
    }
  }

  component PositionRow: BorderSurface {
    id: row
    property var pos: ({})

    implicitHeight: posCol.implicitHeight + Style.space(16)
    color: Color.popups.background
    borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, 1)
    radius: Style.cornerRadius
    clip: true

    Rectangle {
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.bottom: parent.bottom
      width: 3
      color: root.pnlColor(row.pos && row.pos.pnl_usd)
    }

    Column {
      id: posCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(12)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(2)

      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          width: parent.width - pnlText.implicitWidth - parent.spacing
          text: Model.plain(row.pos && (row.pos.wsname || row.pos.pair_id))
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }

        Text {
          id: pnlText
          textFormat: Text.PlainText
          text: Model.plain(Model.fmtSignedUsd(row.pos && row.pos.pnl_usd))
          color: root.pnlColor(row.pos && row.pos.pnl_usd)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: Model.plain(
          "Last " + Model.fmtUsd(row.pos && row.pos.last)
          + "  ·  entry " + Model.fmtUsd(row.pos && row.pos.entry)
          + "  ·  " + Model.fmtPct(row.pos && row.pos.pct_vs_entry))
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
  }

  component ActionButton: Rectangle {
    id: button
    property string label: ""
    property bool enabled: true
    signal clicked()

    implicitHeight: buttonLabel.implicitHeight + Style.space(12)
    radius: Style.cornerRadius
    color: buttonHit.containsMouse && enabled
      ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
      : Qt.rgba(root.accent.r, root.accent.g, root.accent.b, enabled ? 0.10 : 0.04)
    border.width: 1
    border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, enabled ? 0.35 : 0.12)
    opacity: enabled ? 1 : 0.7

    Text {
      id: buttonLabel
      textFormat: Text.PlainText
      anchors.centerIn: parent
      text: button.label
      color: button.enabled ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    MouseArea {
      id: buttonHit
      anchors.fill: parent
      enabled: button.enabled
      hoverEnabled: true
      cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: button.clicked()
    }
  }
}
