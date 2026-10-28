"""Resume the Grok 4.7 chat with a fresh snapshot and let it decide."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import ROOT, desk_cfg, read_chat_id, state_dir, utc_now, write_chat_id

PROMPT = """You are the desk for a small Kraken USD spot account. It is {ts}.
Below is the current book, the market, the rules you already set, and the reason stored for each recent trade.
Decide what to do next. You may buy now, sell now, set an exit rule, or arm a buy that waits for a price or a time.
Holding is a valid decision. Say what you see and what you are doing.

Use only `{bin}` from the repo root. Every order needs a short --reason; that sentence is stored with the trade.
Do not read pass, do not call the Kraken API yourself, and do not edit config.json.
desk.live is {live}. While it is false, orders are recorded as shadow and nothing is sent.

Commands:
  {bin} trade buy --pair PAIR --dollars N --reason "why" [--exit-name stop_pct --exit-params '{{"pct":0.08}}']
  {bin} trade sell --pair PAIR --reason "why"
  {bin} rule set --pair PAIR --name stop_pct|trail|time_stop|volume_fade|none --params '{{"pct":0.08}}' --reason "why"
  {bin} entry add --pair PAIR --dollars N --kind buy_limit|buy_stop|at_time --price P --at ISO --expires ISO --exit-name stop_pct --exit-params '{{"pct":0.08}}' --reason "why"
  {bin} entry cancel --id N --reason "why"
  {bin} halt --reason "why"
  {bin} snapshot

A buy_limit rests at or under --price. A buy_stop rests until price trades up through --price.
at_time buys at --at, and only inside --band-low/--band-high when those are set.
Custom rules: write rules/custom/NAME.py that reads a JSON context on stdin and prints hold, sell, wait, or buy. Then pass --name custom --params '{{"module":"NAME"}}'.
Exit rules already on a position keep running between hours. A waiting buy expires at the next hour unless you set --expires.

Snapshot:
{snapshot}
"""


def agent_bin() -> str | None:
    return shutil.which("cursor-agent") or shutil.which("agent")


def prompt_for(snapshot: dict) -> str:
    return PROMPT.format(
        ts=snapshot.get("ts") or utc_now(),
        bin=str(ROOT / "bin" / "desk"),
        live=str(bool(snapshot.get("live"))).lower(),
        snapshot=json.dumps(snapshot, indent=2, default=str),
    )


def run_wake(snapshot: dict, timeout: int | None = None) -> dict:
    exe = agent_bin()
    cfg_timeout = int(desk_cfg().get("wakeTimeoutSeconds") or 900)
    timeout = cfg_timeout if timeout is None else timeout
    model = desk_cfg().get("model") or "grok-4.7-high"
    if not exe:
        return {"ok": False, "error": "cursor-agent not found", "model": model}
    session = read_chat_id()
    cmd = [exe, "-p", "--output-format", "stream-json", "--model", model, "--workspace", str(ROOT),
           "--trust", "--force", "--sandbox", "disabled"]
    if session:
        cmd += ["--resume", session]
    cmd.append(prompt_for(snapshot))
    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "")
    transcript = log_dir / f"wake-{stamp}.jsonl"
    stderr = log_dir / f"wake-{stamp}.err"
    timed_out = False
    with transcript.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
    new_id = _session_id(transcript) or session
    if new_id:
        write_chat_id(new_id)
    text = _assistant_text(transcript)
    return {
        "ok": not timed_out and proc.returncode == 0,
        "timed_out": timed_out,
        "exit_code": proc.returncode,
        "model": model,
        "session_id": new_id,
        "summary": text[-2000:],
        "transcript": str(transcript),
    }


def open_chat() -> None:
    """Replace this process with the interactive Grok chat."""
    exe = agent_bin()
    if not exe:
        raise SystemExit("cursor-agent not found")
    model = desk_cfg().get("model") or "grok-4.7-high"
    session = read_chat_id()
    cmd = [exe, "--model", model, "--workspace", str(ROOT), "--trust"]
    if session:
        cmd += ["--resume", session]
    else:
        cmd.append(
            "You are the Kraken desk. Trade only through bin/desk "
            "(trade, rule set, entry add, halt, snapshot). Every order needs --reason. "
            "Nothing is sent to Kraken until desk.live is true."
        )
    os.execv(exe, cmd)


def _session_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("session_id"):
            return str(row["session_id"])
    return None


def _assistant_text(path: Path) -> str:
    if not path.is_file():
        return ""
    chunks: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


def _kill(proc: subprocess.Popen) -> None:
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
