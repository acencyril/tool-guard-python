"""Tool Guard in front of an MCP server you did not write (Gmail, Notion, a bank, a drive...).

The agent talks to the gateway; the gateway starts the real server and relays every message, except:
  - tools/call: the rules of the config are checked first. Refused or held: the call never reaches the server,
    and the model gets Tool Guard's message as the tool's answer.
  - tools/list: every tool is fingerprinted (name, description, input schema). A tool that appears or changes
    after it was trusted, or whose description gives the model instructions, cannot be called until a person
    trusts it again (`tool-guard gateway --config ... trust NAME`).
Approved requests run here, at the next check, and their result is stored with the request.

Config (JSON):
{
  "upstream": {"command": "npx", "args": ["-y", "some-mcp-server@1.4.2"], "env": {"API_KEY": "..."}},
  "state_dir": "var/tool-guard/gmail",
  "default": "allow",                       # tools without a rule: allow | approval | deny
  "trust_on_first_use": true,               # the first tools/list is trusted as it is
  "tools": {
    "send_email": {"allowed_values": {"to": ["*@mycompany.com"]}},
    "delete_*": {"deny": "deleting mail is not allowed"},
    "create_payment": {"limit": {"amount": {"max": 50, "above": "approval", "min": 0}}},
    "search": {"rate": {"limit": 30, "per_seconds": 60}}
  },
  "agent": "assistant", "user": "cyril"     # who the calls are written down for (optional)
}
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from .guard import Guard
from .rules import APPROVAL, DENY, AllowedValues, Limit, RateLimit, RequiresApproval, Rules  # noqa: F401
from .stores import APPROVED, FileApprovals, FileCounter, FileJournal, FileStops, _locked, _write_atomic, now_iso

# A description that addresses the model instead of describing the tool: the "poisoned tool" pattern.
SUSPICIOUS = [
    r"ignore (all |any )?(the |your )?(previous|prior|above) (instructions|rules)",
    r"(do not|don't|never) (tell|inform|mention|reveal)[^.]{0,40}(user|human)",
    r"<\s*(important|system|instructions?)\s*>",
    r"(send|forward|copy|upload|post)[^.]{0,60}(to|at) (https?://|[\w.+-]+@[\w-]+\.)",
    r"(read|open|include|attach)[^.]{0,40}(\.ssh|id_rsa|\.env|password|api[_ -]?key|token|credentials)",
    r"before (using|calling) (this|any other) tool",
    r"ignore[sz]? (toutes |les )?(instructions|consignes) (précédentes|ci-dessus)",
    r"ne (le )?dis pas à l'utilisateur",
]


def fingerprint(tool: dict[str, Any]) -> str:
    keep = {"name": tool.get("name"), "description": tool.get("description"), "inputSchema": tool.get("inputSchema")}
    # compact JSON, keys sorted: the same fingerprint as the PHP and Node gateways
    return hashlib.sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def suspicious(tool: dict[str, Any]) -> str | None:
    text = json.dumps({"d": tool.get("description"), "s": tool.get("inputSchema")}, ensure_ascii=False).lower()
    for pattern in SUSPICIOUS:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return None


class Pins:
    """The tools a person trusts, as they were when trusted."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text() or "{}") if self.path.exists() else {}
        except ValueError:
            return {"_corrupt": True}

    def save(self, data: dict[str, Any]) -> None:
        _write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    def review(self, tools: list[dict[str, Any]], trust_on_first_use: bool) -> dict[str, str]:
        """Compare what the server lists with what was trusted; returns {tool: why it cannot be called}."""
        with _locked(self.path):
            data = self.load()
            if data.get("_corrupt"):
                return {t.get("name", "?"): "the trusted tools file cannot be read" for t in tools}
            first = not data.get("trusted")
            data.setdefault("trusted", {})
            data["seen"] = {}
            blocked: dict[str, str] = {}
            for t in tools:
                name, fp = str(t.get("name")), fingerprint(t)
                bad = suspicious(t)
                data["seen"][name] = {"fingerprint": fp, "description": t.get("description"), "inputSchema": t.get("inputSchema"),
                                      "suspicious": bad}
                known = data["trusted"].get(name)
                if known and known["fingerprint"] == fp:
                    continue
                if first and trust_on_first_use and not bad:
                    data["trusted"][name] = {"fingerprint": fp, "at": now_iso(), "by": "first use"}
                    continue
                if bad:
                    blocked[name] = f'tool "{name}" is not trusted: its description gives the model instructions ("{bad}")'
                elif known:
                    blocked[name] = f'tool "{name}" changed since it was trusted on {known["at"][:10]}; a person must review it'
                else:
                    blocked[name] = f'tool "{name}" is new and not trusted yet; a person must review it'
            data["blocked"] = blocked
            self.save(data)
            return blocked

    def trust(self, names: list[str] | None, by: str) -> list[str]:
        with _locked(self.path):
            data = self.load()
            seen = data.get("seen", {})
            done = []
            for name, t in seen.items():
                if names is None or name in names:
                    data.setdefault("trusted", {})[name] = {"fingerprint": t["fingerprint"], "at": now_iso(), "by": by}
                    data.get("blocked", {}).pop(name, None)
                    done.append(name)
            self.save(data)
            return done


def rules_for(config: dict[str, Any], tool: str) -> tuple[Rules, str | None]:
    """The rules of one upstream tool, from the config, and the reason when the tool is simply refused."""
    tools = config.get("tools") or {}
    spec = tools.get(tool)
    if spec is None:
        spec = next((v for k, v in tools.items() if fnmatch.fnmatchcase(tool, k)), None)
    r = Rules()
    if spec is None:
        default = config.get("default", "allow")
        if default == "deny":
            spec = {"deny": f'"{tool}" has no rule and the default is to refuse'}
        elif default == "approval":
            spec = {"approval": f'"{tool}" has no rule and the default is a human approval'}
        else:
            return r, None
    if "deny" in spec:
        return r, str(spec["deny"])
    for arg, lim in (spec.get("limit") or {}).items():
        r.limits.append(Limit(arg, float(lim["max"]), lim.get("above", DENY), None if lim.get("min") is None else float(lim["min"])))
    for arg, av in (spec.get("allowed_values") or {}).items():
        patterns = av if isinstance(av, list) else av.get("patterns", [])
        otherwise = APPROVAL if isinstance(av, list) else av.get("otherwise", APPROVAL)
        r.values.append(AllowedValues(arg, tuple(patterns), otherwise))
    if spec.get("rate"):
        r.rate = RateLimit(int(spec["rate"]["limit"]), int(spec["rate"]["per_seconds"]), spec["rate"].get("per", "user"))
    if "approval" in spec:
        r.approval = RequiresApproval(spec["approval"] if isinstance(spec["approval"], str) else None)
    return r, None


def build_guard(config: dict[str, Any], base: Path) -> tuple[Guard, Pins]:
    state = (base / config.get("state_dir", "var/tool-guard")).resolve()
    g = Guard(approvals=FileApprovals(state / "approvals"), journal=FileJournal(state / "journal.jsonl"),
              stops=FileStops(state / "stops.json"), counter=FileCounter(state / "rate.json"),
              max_pending_per_user=int(config.get("max_pending_per_user", 3)), expire_after=config.get("expire_after"),
              four_eyes=bool(config.get("four_eyes", False)))
    g.deferred = True
    return g, Pins(state / "pins.json")


def load_config(path: str) -> tuple[dict[str, Any], Path]:
    p = Path(path)
    return json.loads(p.read_text()), p.resolve().parent


class Gateway:
    def __init__(self, config: dict[str, Any], base: Path, client_in: TextIO = sys.stdin, client_out: TextIO = sys.stdout,
                 poll: float = 2.0) -> None:
        self.config, self.base = config, base
        self.guard, self.pins = build_guard(config, base)
        self.client_in, self.client_out = client_in, client_out
        self.poll = poll
        self.out_lock, self.up_lock = threading.Lock(), threading.Lock()
        self.list_ids: set[Any] = set()
        self.internal: dict[str, str] = {}  # our own request id -> approval id
        self.blocked: dict[str, str] = {}
        self.ready = threading.Event()
        self.done = threading.Event()
        self.proc: subprocess.Popen[str] | None = None

    # ---- plumbing ----
    def to_client(self, msg: dict[str, Any]) -> None:
        with self.out_lock:
            self.client_out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.client_out.flush()

    def to_upstream(self, msg: dict[str, Any] | str) -> None:
        assert self.proc and self.proc.stdin
        line = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
        with self.up_lock:
            self.proc.stdin.write(line.rstrip("\n") + "\n")
            self.proc.stdin.flush()

    def run(self) -> int:
        up = self.config["upstream"]
        env = {**os.environ, **{k: str(v) for k, v in (up.get("env") or {}).items()}}
        self.proc = subprocess.Popen([up["command"], *up.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1, env=env, cwd=up.get("cwd") or None)
        threads = [threading.Thread(target=self.from_upstream, daemon=True), threading.Thread(target=self.approved_runner, daemon=True)]
        for t in threads:
            t.start()
        try:
            self.from_client()
        finally:
            self.done.set()
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return 0

    # ---- client -> server ----
    def from_client(self) -> None:
        for line in self.client_in:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.to_upstream(line)
                continue
            if isinstance(msg, dict) and msg.get("method") == "tools/list" and "id" in msg:
                self.list_ids.add(msg["id"])
            if isinstance(msg, dict) and msg.get("method") == "notifications/initialized":
                self.to_upstream(msg)
                self.ready.set()
                continue
            if isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg:
                answer = self.check(msg)
                if answer is not None:
                    self.to_client(answer)
                    continue
            self.to_upstream(msg)

    def check(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        params = msg.get("params") or {}
        tool, args = str(params.get("name")), params.get("arguments") or {}
        decision = self.decide(tool, args if isinstance(args, dict) else {})
        if decision.allowed:
            return None
        return {"jsonrpc": "2.0", "id": msg["id"],
                "result": {"content": [{"type": "text", "text": decision.message_for_model()}], "isError": True}}

    def decide(self, tool: str, args: dict[str, Any]):
        from .decision import Decision
        reason = self.blocked.get(tool) or self.pins.load().get("blocked", {}).get(tool)
        rules, refused = rules_for(self.config, tool)
        reason = reason or refused
        if reason:
            d = Decision.deny(reason)
            self.guard.journal.record({"event": "call", "channel": "ai", "tool": tool, "user": self.config.get("user"),
                                       "arguments": args, "verdict": d.verdict, "reason": reason, "approval": None,
                                       **({"agent": self.config["agent"]} if self.config.get("agent") else {})})
            return d
        return self.guard.decide(tool, args, rules=rules, user=self.config.get("user"), agent=self.config.get("agent"))

    # ---- server -> client ----
    def from_upstream(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                with self.out_lock:
                    self.client_out.write(line)
                    self.client_out.flush()
                continue
            mid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(mid, str) and mid in self.internal:
                self.finish(self.internal.pop(mid), msg)
                continue
            if mid in self.list_ids:
                self.list_ids.discard(mid)
                tools = (msg.get("result") or {}).get("tools") or []
                self.blocked = self.pins.review(tools, bool(self.config.get("trust_on_first_use", True)))
                for name, why in self.blocked.items():
                    self.guard.journal.record({"event": "untrusted", "tool": name, "reason": why})
            self.to_client(msg)
        self.done.set()

    # ---- approved requests run here ----
    def approved_runner(self) -> None:
        while not self.done.is_set():
            if self.ready.is_set():
                for a in self.guard.approvals.all(APPROVED):
                    if a.queued and a.id not in self.internal.values():
                        rid = f"tool-guard-{a.id}"
                        self.internal[rid] = a.id
                        self.to_upstream({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": a.tool, "arguments": a.arguments}})
            self.done.wait(self.poll)

    def finish(self, approval_id: str, msg: dict[str, Any]) -> None:
        a = self.guard.approvals.get(approval_id)
        if a is None:
            return
        if "error" in msg:
            a.status, a.result = "failed", json.dumps(msg["error"], ensure_ascii=False)[:2000]
        else:
            content = (msg.get("result") or {}).get("content") or []
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            a.result = (text or json.dumps(msg.get("result"), ensure_ascii=False))[:2000]
            if (msg.get("result") or {}).get("isError"):
                a.status = "failed"
        a.queued = False
        self.guard.approvals.save(a)
        self.guard.journal.record({"event": "approved_run", "tool": a.tool, "approval": a.id, "status": a.status})
