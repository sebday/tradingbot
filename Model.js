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
  if (abs >= 10000) {
    var s = String(Math.round(abs))
    var out = ""
    for (var i = 0; i < s.length; i++) {
      if (i > 0 && (s.length - i) % 3 === 0) out += ","
      out += s.charAt(i)
    }
    return sign + "$" + out
  }
  return sign + "$" + abs.toFixed(2)
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

function barValue(data) {
  if (!data || !data.ok) return ""
  return String(data.text || "")
}

function barTooltip(data, loading) {
  if (loading) return "Refreshing desk…"
  if (!data || !data.ok) return plain(data && data.error ? data.error : "Kraken desk")
  return plain(data.tooltip || "Kraken desk")
}

function cycleSummary(cycle) {
  if (!cycle || typeof cycle !== "object") return "No cycle report yet"
  var parts = []
  if (cycle.ts) parts.push(String(cycle.ts))
  if (cycle.equity_usd != null) parts.push("equity " + fmtUsd(cycle.equity_usd))
  if (cycle.vet_pass != null) parts.push("vet " + cycle.vet_pass)
  if (cycle.risk_closes != null && cycle.risk_closes > 0)
    parts.push("closes " + cycle.risk_closes)
  if (cycle.note) parts.push(plain(cycle.note, 280))
  return parts.join(" · ")
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

function cycleMode(cycle) {
  if (!cycle || typeof cycle !== "object") return ""
  if (cycle.paper === true) return "Paper"
  if (cycle.degraded === true) return "Degraded"
  return "Live"
}

function cycleStats(cycle) {
  if (!cycle || typeof cycle !== "object") return ""
  var parts = []
  var scan = cycle.scan != null ? cycle.scan : cycle.candidates
  if (scan != null) parts.push("scan " + scan)
  var vet = cycle.vet_pass != null ? cycle.vet_pass : cycle.passes
  if (vet != null) parts.push("vet " + vet)
  if (cycle.fills != null) parts.push("fills " + cycle.fills)
  var closes = cycle.risk_closes != null ? cycle.risk_closes : cycle.closes
  if (closes != null) parts.push("closes " + closes)
  if (cycle.risk_holds != null) parts.push("holds " + cycle.risk_holds)
  return parts.join(" · ")
}

function cycleNote(cycle) {
  if (!cycle || typeof cycle !== "object") return ""
  return plain(cycle.note || "", 280)
}
