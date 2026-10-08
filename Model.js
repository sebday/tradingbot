.pragma library

function plain(value, maxLen) {
  var s = String(value == null ? "" : value)
  var max = maxLen || 400
  var out = ""
  for (var i = 0; i < s.length && out.length < max; i++) {
    var code = s.charCodeAt(i)
    if (code < 32 || (code >= 127 && code < 160)) continue
    var c = s.charAt(i)
    if (c === "<" || c === ">" || c === "&") continue
    out += c
  }
  return out
}

function fmtUsd(val) {
  var n = parseFloat(val)
  if (isNaN(n)) return "—"
  var sign = n < 0 ? "-" : ""
  var abs = Math.abs(n)
  if (abs < 1) return sign + "$" + abs.toFixed(2)
  var rounded = String(Math.round(abs))
  var out = ""
  for (var i = 0; i < rounded.length; i++) {
    if (i > 0 && (rounded.length - i) % 3 === 0) out += ","
    out += rounded.charAt(i)
  }
  return sign + "$" + out
}

function fmtSignedUsd(val) {
  var n = parseFloat(val)
  if (isNaN(n)) return "—"
  if (n > 0) return "+" + fmtUsd(n)
  return fmtUsd(n)
}

function clock(ts) {
  var s = String(ts || "")
  var t = s.indexOf("T")
  if (t >= 0 && s.length >= t + 6)
    return s.substring(t + 1, t + 6)
  return plain(s, 16)
}

function fmtPct(val) {
  var n = parseFloat(val)
  if (isNaN(n)) return "—"
  return (n >= 0 ? "+" : "") + n.toFixed(1) + "%"
}

function emptyData(error) {
  return {
    ok: false,
    error: String(error || ""),
    text: "",
    tooltip: "Kraken desk",
    equity_usd: null,
    usd_spot: null,
    positions: [],
    last_cycle: null,
    runner: {},
    halted: false
  }
}

function parsePayload(raw) {
  var text = String(raw || "").trim()
  if (!text) return emptyData("No data")
  try {
    var json = JSON.parse(text)
  } catch (e) {
    return emptyData("Invalid response")
  }
  if (json.class === "error" || json.ok === false) {
    return emptyData(json.error || json.message || "Unavailable")
  }
  return {
    ok: true,
    error: "",
    text: String(json.text || ""),
    tooltip: String(json.tooltip || "Kraken desk"),
    equity_usd: json.equity_usd,
    usd_spot: json.usd_spot,
    allocated_usd: json.allocated_usd,
    halt_floor_usd: json.halt_floor_usd,
    halted: json.halted === true,
    open_count: json.open_count || 0,
    positions: Array.isArray(json.positions) ? json.positions : [],
    last_cycle: json.last_cycle || null,
    runner: json.runner && typeof json.runner === "object" ? json.runner : {},
    desk_root: String(json.desk_root || ""),
    balance_error: json.balance_error || null
  }
}

function deskIcon() {
  return "\uf201"
}

function barAmount(data) {
  if (!data || !data.ok) return ""
  if (data.equity_usd !== undefined && data.equity_usd !== null && data.equity_usd !== "")
    return fmtUsd(data.equity_usd)
  return plain(data.text, 32)
}

function barValue(data) {
  var amount = barAmount(data)
  if (amount) return deskIcon() + " " + amount
  return deskIcon()
}

function barTooltip(data, loading) {
  if (loading) return "Refreshing desk…"
  if (!data || !data.ok) return plain(data && data.error ? data.error : "Kraken desk")
  if (data.equity_usd !== undefined && data.equity_usd !== null && data.equity_usd !== "")
    return plain("Desk equity " + fmtUsd(data.equity_usd))
  return plain(data.tooltip || "Kraken desk")
}

function runnerLine(runner) {
  if (!runner || typeof runner !== "object") return "No cycle yet"
  if (runner.running) return "Cycle running"
  if (runner.stale) return "Over 70m ago"
  if (runner.finished_at) return "Last cycle " + clock(runner.finished_at)
  return "No cycle yet"
}

function deskBadge(data, cycleRunning) {
  if (!data || data.ok !== true) return ""
  if (data.halted) return "Halted"
  if (cycleRunning || (data.runner && data.runner.running)) return "Running"
  if (data.runner && data.runner.stale) return "Stale"
  if (data.last_cycle && data.last_cycle.paper === true) return "Paper"
  return "Live"
}

function heroMeta(data) {
  if (!data || data.ok !== true) return "Kraken desk"
  var n = parseInt(data.open_count, 10)
  if (isNaN(n))
    n = data.positions instanceof Array ? data.positions.length : 0
  var open = n === 1 ? "1 open" : (n + " open")
  return open + " · " + runnerLine(data.runner)
}

function unrealizedUsd(positions) {
  if (!(positions instanceof Array)) return null
  var sum = 0
  var any = false
  for (var i = 0; i < positions.length; i++) {
    var n = parseFloat(positions[i] && positions[i].pnl_usd)
    if (isNaN(n)) continue
    sum += n
    any = true
  }
  if (!any) return null
  return Math.round(sum * 100) / 100
}
