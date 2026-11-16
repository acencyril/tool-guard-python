"""Command line, for the human side (and `tool-guard gateway --config FILE ...` for the MCP gateway):

    tool-guard --guard myapp.agent:guard approvals            list what waits
    tool-guard --guard myapp.agent:guard approve 3f9a1c2e --by sophie
    tool-guard --guard myapp.agent:guard reject 3f9a1c2e --by sophie
    tool-guard --guard myapp.agent:guard stop [all|agents|tool NAME|agent NAME] --by cyril --reason "incident"
    tool-guard --guard myapp.agent:guard resume [all|agents|tool NAME|agent NAME]
    tool-guard --guard myapp.agent:guard rules
    tool-guard --guard myapp.agent:guard journal [-n 20]
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from .guard import Guard


def load(spec: str) -> Guard:
    module, _, attr = spec.partition(":")
    obj = getattr(importlib.import_module(module), attr or "guard")
    if not isinstance(obj, Guard):
        raise SystemExit(f"{spec} is not a tool_guard.Guard")
    return obj


def gateway_main(argv: list[str]) -> int:
    """tool-guard gateway --config gmail.json [run|tools|trust NAME...|trust --all|approvals|approve ID --by X|reject ID --by X|stop ...|resume ...|journal]"""
    from .gateway import Gateway, build_guard, load_config

    p = argparse.ArgumentParser(prog="tool-guard gateway")
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("tools")
    t = sub.add_parser("trust")
    t.add_argument("names", nargs="*")
    t.add_argument("--all", action="store_true")
    t.add_argument("--by", default="cli")
    sub.add_parser("approvals")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.add_argument("--by", required=True)
    for name in ("stop", "resume"):
        s = sub.add_parser(name)
        s.add_argument("scope", nargs="?", default="all", choices=["all", "agents", "tool", "agent"])
        s.add_argument("target", nargs="?")
        s.add_argument("--by")
        if name == "stop":
            s.add_argument("--reason")
    j = sub.add_parser("journal")
    j.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)
    config, base = load_config(a.config)
    if a.cmd in (None, "run"):
        return Gateway(config, base, poll=float(config.get("poll_seconds", 2.0))).run()
    guard, pins = build_guard(config, base)
    if a.cmd == "tools":
        data = pins.load()
        for name, t in sorted(data.get("seen", {}).items()):
            state = "BLOCKED: " + data["blocked"][name] if name in data.get("blocked", {}) else "trusted"
            print(f"{name}: {state}")
        if not data.get("seen"):
            print("No tool listed yet: start the agent once through the gateway.")
        return 0
    if a.cmd == "trust":
        if not a.names and not a.all:
            print("Name the tools to trust, or --all.", file=sys.stderr)
            return 2
        done = pins.trust(None if a.all else a.names, a.by)
        print("Trusted: " + (", ".join(done) if done else "nothing (unknown names?)"))
        return 0 if done else 1
    return _human(guard, a)


def _human(guard: Guard, a: argparse.Namespace) -> int:
    if a.cmd == "approvals":
        waiting = guard.approvals.all("pending")
        for x in waiting:
            late = " (expired)" if x.expired(guard.expire_after) else ""
            print(f"{x.id}  {x.requested_at}  {x.tool}({json.dumps(x.arguments, ensure_ascii=False)})  by {x.requested_by or '-'}: {x.reason}{late}")
        if not waiting:
            print("Nothing waits for a human.")
    elif a.cmd in ("approve", "reject"):
        try:
            done = guard.approve(a.id, a.by) if a.cmd == "approve" else guard.reject(a.id, a.by)
        except (LookupError, ValueError, PermissionError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{done.id}: {done.status}" + (f" → {done.result}" if done.result else ""))
        return 0 if done.status != "failed" else 1
    elif a.cmd == "stop":
        guard.stop(a.scope, a.target, by=a.by, reason=a.reason)
        print("Stopped.")
    elif a.cmd == "resume":
        print("Resumed." if guard.resume(a.scope, a.target, by=a.by) else "Nothing was stopped there.")
    elif a.cmd == "journal":
        tail = getattr(guard.journal, "tail", None)
        for e in (tail(a.n) if tail else guard.journal.entries[-a.n:]):
            print(json.dumps(e, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["gateway"]:
        return gateway_main(argv[1:])
    p = argparse.ArgumentParser(prog="tool-guard")
    p.add_argument("--guard", required=True, help="module:attribute of your Guard, e.g. myapp.agent:guard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("approvals")
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.add_argument("--by", required=True)
    for name in ("stop", "resume"):
        s = sub.add_parser(name)
        s.add_argument("scope", nargs="?", default="all", choices=["all", "agents", "tool", "agent"])
        s.add_argument("target", nargs="?")
        s.add_argument("--by")
        if name == "stop":
            s.add_argument("--reason")
    sub.add_parser("rules")
    j = sub.add_parser("journal")
    j.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)
    guard = load(a.guard)
    if a.cmd == "rules":
        for tool, lines in guard.rules().items():
            print(tool + (":" if lines else ": no rule"))
            for line in lines:
                print("  - " + line)
        return 0
    return _human(guard, a)


if __name__ == "__main__":
    raise SystemExit(main())
