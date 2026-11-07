"""Hand-rolled stdio MCP server (newline-delimited JSON-RPC 2.0).

Supports initialize, notifications/initialized, ping, tools/list, tools/call.
No shell, no file access for the seat: only DeskTools.
"""
from __future__ import annotations

import json
import sys

from .. import HARNESS_VERSION
from .tools import TOOL_SPECS, DeskTools

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class Server:
    def __init__(self, tools: DeskTools | None = None):
        self._tools = tools
        self.initialized = False

    @property
    def tools(self) -> DeskTools:
        if self._tools is None:
            self._tools = DeskTools()
        return self._tools

    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        is_note = "id" not in msg
        try:
            if method == "initialize":
                want = (msg.get("params") or {}).get("protocolVersion")
                ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                self.initialized = True
                return _ok(mid, {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "desk", "version": HARNESS_VERSION},
                                 "instructions": "Trading desk seat tools. Start with get_mandate."})
            if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
                return None
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": TOOL_SPECS})
            if method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                if name not in {t["name"] for t in TOOL_SPECS}:
                    return _err(mid, -32602, f"unknown tool {name}")
                result = self.tools.call(name, params.get("arguments") or {})
                return _ok(mid, {"content": [{"type": "text", "text": json.dumps(result, default=str)}],
                                 "structuredContent": result, "isError": not result.get("ok")})
            if is_note:
                return None
            return _err(mid, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001
            if is_note:
                return None
            return _err(mid, -32603, f"{type(exc).__name__}: {exc}"[:300])


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None, server: Server | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = server or Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _err(None, -32700, "parse error")
        else:
            if isinstance(msg, list):
                resps = [r for r in (server.handle(m) for m in msg if isinstance(m, dict)) if r is not None]
                if resps:
                    stdout.write(json.dumps(resps) + "\n")
                    stdout.flush()
                continue
            resp = server.handle(msg) if isinstance(msg, dict) else _err(None, -32600, "invalid request")
        if resp is not None:
            stdout.write(json.dumps(resp, default=str) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
