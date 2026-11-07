"""Run the trader seat: cursor-agent in seat/ with MCP tools only, one wall-clock deadline.

The seat gets no keys: its environment is scrubbed to a short allowlist. Bounded repair is
enforced by the MCP server (maxSubmissions); the runner enforces the deadline by killing
the process group. Transcript (stream-json) goes to runs/<cycle_id>/transcript.jsonl.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .. import paths
from ..util import sha256_text

PROMPT = ("Desk cycle {cycle_id}. You are the trader seat; follow AGENTS.md. "
          "Call get_mandate first. Gather evidence with the desk tools, then call submit_proposals exactly once "
          "with cycle_id {cycle_id} (fix and resubmit only if it reports errors). Holding is a valid decision. "
          "Deadline {deadline}. Reply with a three-line summary when done.")

ENV_ALLOW = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
             "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "DBUS_SESSION_BUS_ADDRESS", "TZ",
             "DESK_STATE_DIR", "DESK_DB", "DESK_RUNS_DIR", "DESK_CONFIG", "DESK_FAKE_MARKET", "DESK_FAKE_CONTEXT",
             "DESK_SEAT_DIR")


def agent_bin() -> str | None:
    return shutil.which("cursor-agent") or shutil.which("agent")


def prompt_for(mandate) -> str:
    return PROMPT.format(cycle_id=mandate.cycle_id, deadline=mandate.deadline)


def prompt_hash(mandate) -> str:
    agents = (paths.seat_dir() / "AGENTS.md")
    text = agents.read_text(encoding="utf-8") if agents.exists() else ""
    return sha256_text(PROMPT + "\n---\n" + text)


class CursorSeat:
    name = "cursor-agent"

    def __init__(self, cfg: dict):
        seat = (cfg.get("harness") or {}).get("seat") or {}
        self.model = seat.get("model") or "composer-2.5"
        self.timeout = int(seat.get("timeoutSeconds") or 600)
        self.sandbox = seat.get("sandbox", "enabled")

    def run(self, mandate, run_dir: Path) -> dict:
        exe = agent_bin()
        if not exe:
            return {"ok": False, "error": "cursor-agent not found", "model": self.model}
        cmd = [exe, "-p", "--output-format", "stream-json", "--workspace", str(paths.seat_dir()), "--trust",
               "--approve-mcps"]
        if self.model and self.model != "inherit":
            cmd += ["--model", self.model]
        if self.sandbox in ("enabled", "disabled"):
            cmd += ["--sandbox", self.sandbox]
        cmd.append(prompt_for(mandate))
        env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
        transcript = run_dir / "transcript.jsonl"
        stderr = run_dir / "seat.stderr.log"
        t0 = time.time()
        timed_out = False
        with transcript.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, cwd=str(paths.seat_dir()), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    env=env, start_new_session=True)
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        return {"ok": not timed_out and proc.returncode == 0, "exit_code": proc.returncode, "timed_out": timed_out,
                "duration_s": round(time.time() - t0, 1), "model": self.model, "cmd": cmd[:-1] + ["<prompt>"]}


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


class ScriptedSeat:
    """Fake seat for tests and dry runs: drives DeskTools in-process through the MCP Server.

    script(call) -> None, where call(name, args) returns the tool result dict.
    """
    name = "scripted"

    def __init__(self, script, tools_factory=None):
        self.script = script
        self.tools_factory = tools_factory

    def run(self, mandate, run_dir: Path) -> dict:
        from .mcp_server import Server
        server = Server(self.tools_factory() if self.tools_factory else None)
        server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        seq = [0]
        lines = []

        def call(name, args=None):
            seq[0] += 1
            req = {"jsonrpc": "2.0", "id": seq[0], "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
            resp = server.handle(req)
            lines.append({"req": req, "resp": resp})
            if "error" in resp:
                return {"ok": False, "error": {"code": "RPC", "message": resp["error"].get("message")}}
            return resp["result"]["structuredContent"]

        t0 = time.time()
        self.script(call, mandate)
        import json
        (run_dir / "transcript.jsonl").write_text("\n".join(json.dumps(x, default=str) for x in lines) + "\n")
        return {"ok": True, "exit_code": 0, "timed_out": False, "duration_s": round(time.time() - t0, 2),
                "model": "scripted"}


def hold_script(call, mandate):
    """Default dry seat: look, then hold."""
    r = call("get_mandate")
    call("get_portfolio")
    call("submit_proposals", {"cycle_id": mandate.cycle_id,
                              "proposals": [{"action": "hold", "thesis": "dry seat: hold", "evidence": [r["tool_call_id"]]}]})


def baseline_script(call, mandate):
    """Mechanical baseline seat: open the top-ranked eligible pair at the ticket cap with the max-wide stop."""
    m = call("get_mandate")
    u = call("get_universe")
    p = call("get_portfolio")
    ev = [m["tool_call_id"], u["tool_call_id"], p["tool_call_id"]]
    data = m.get("data") or {}
    props = []
    if not data.get("observe_only") and "open" in data.get("allowed_actions", []) and u.get("data"):
        top = u["data"][0]
        band = data.get("stop_band") or {}
        stop = float(top["ask"]) * (1 - float(band.get("maxStopPct", 0.08)) + 0.0001)
        size = min(float(data.get("ticket_cap_usd") or 0), float(data.get("budget_usd") or 0))
        if size > 0:
            props.append({"action": "open", "pair_id": top["pair_id"], "size_usd": round(size, 2), "stop_price": stop,
                          "thesis": "baseline: top-ranked eligible pair", "invalidation": "stop", "evidence": ev})
    if not props:
        props = [{"action": "hold", "thesis": "baseline: nothing eligible", "evidence": ev}]
    call("submit_proposals", {"cycle_id": mandate.cycle_id, "proposals": props})
