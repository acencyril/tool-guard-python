"""The guard: reads the rules written on a tool and decides, before the tool runs, whether it may.

    guard = Guard(approvals=FileApprovals("var/approvals"), journal=FileJournal("var/journal.jsonl"))

    @guard.tool()
    @limit("amount", max=50, above=APPROVAL)
    def refund(order: str, amount: float) -> str:
        ...

The model calls `refund` as usual. Within the rules, it runs. Otherwise it does not run, and the model
gets a plain message telling it not to retry and to tell the user (refused, or waiting for a human).
"""

from __future__ import annotations

import functools
import inspect
import math
from typing import Any, Callable

from .decision import Decision, ToolCallBlocked
from .rules import DENY as ABOVE_DENY, PER_TOOL, Rules, rules_of
from .stores import (APPROVED, FAILED, PENDING, REJECTED, Approval, MemoryApprovals, MemoryCounter, MemoryJournal,
                     Stop, now_iso)

ENFORCE = "enforce"
OBSERVE = "observe"
AI = "ai"
CODE = "code"


class Guard:
    def __init__(
        self,
        *,
        approvals: Any = None,
        journal: Any = None,
        stops: Any = None,
        counter: Any = None,
        is_granted: Callable[[str, Any, str | None], bool] | None = None,
        current_user: Callable[[], str | None] | None = None,
        mode: str = ENFORCE,
        max_pending_per_user: int = 3,
        expire_after: int | None = None,
        agents: dict[str, list[str]] | None = None,
        allow_unknown_agent: bool = False,
        four_eyes: bool = False,
    ) -> None:
        if mode not in (ENFORCE, OBSERVE):
            raise ValueError('mode must be "enforce" or "observe"')
        if max_pending_per_user < 1:
            raise ValueError("max_pending_per_user must be at least 1")
        self.approvals = approvals if approvals is not None else MemoryApprovals()
        self.journal = journal if journal is not None else MemoryJournal()
        self.stops = stops
        self.counter = counter if counter is not None else MemoryCounter()
        self.is_granted = is_granted
        self.current_user = current_user
        self.mode = mode
        self.max_pending_per_user = max_pending_per_user
        self.expire_after = expire_after
        self.agents = agents or {}
        self.allow_unknown_agent = allow_unknown_agent
        self.four_eyes = four_eyes
        self._tools: dict[str, Callable[..., Any]] = {}

    # ---- declaring tools ----------------------------------------------------------------------

    def register(self, fn: Callable[..., Any], name: str | None = None) -> str:
        name = name or fn.__name__
        self._tools[name] = fn
        return name

    def tool(self, name: str | None = None, *, raise_blocked: bool = False, user: Callable[[], str | None] | None = None,
             agent: str | None = None):
        """Decorator: the function keeps its name, signature and docstring (agent frameworks read them),
        but every call goes through the guard. Blocked: returns the message for the model, or raises
        ToolCallBlocked with raise_blocked=True."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = self.register(fn, name)
            sig = inspect.signature(fn)

            def arguments(args: tuple, kwargs: dict) -> dict[str, Any]:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                return dict(bound.arguments)

            def who() -> str | None:
                return user() if user else None

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    a = arguments(args, kwargs)
                    decision = self.decide(tool_name, a, rules=rules_of(fn), user=who(), agent=agent)
                    if not decision.allowed:
                        if raise_blocked:
                            raise ToolCallBlocked(decision, tool_name)
                        return decision.message_for_model()
                    return await fn(**a)
                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                a = arguments(args, kwargs)
                decision = self.decide(tool_name, a, rules=rules_of(fn), user=who(), agent=agent)
                if not decision.allowed:
                    if raise_blocked:
                        raise ToolCallBlocked(decision, tool_name)
                    return decision.message_for_model()
                return fn(**a)
            return wrapper
        return deco

    def call(self, tool: str | Callable[..., Any], arguments: dict[str, Any], *, user: str | None = None,
             agent: str | None = None, channel: str = AI) -> Any:
        """Run a tool through the guard with named arguments; raises ToolCallBlocked when it may not run."""
        fn = self._tools[tool] if isinstance(tool, str) else tool
        name = tool if isinstance(tool, str) else next((n for n, f in self._tools.items() if f is fn), fn.__name__)
        decision = self.decide(name, arguments, rules=rules_of(fn), user=user, agent=agent, channel=channel)
        if not decision.allowed:
            raise ToolCallBlocked(decision, name)
        return getattr(fn, "__wrapped__", fn)(**arguments)

    # ---- deciding -----------------------------------------------------------------------------

    def decide(self, tool: str, arguments: dict[str, Any], *, rules: Rules | None = None, user: str | None = None,
               agent: str | None = None, channel: str = AI) -> Decision:
        if user is None and self.current_user is not None:
            user = self.current_user()
        if rules is None:
            rules = rules_of(self._tools[tool]) if tool in self._tools else Rules()
        stopped = self._stopped(tool, agent, channel == AI)
        decision = stopped or self._evaluate(tool, rules, arguments, user, agent, channel)
        would = None
        if stopped is None and self.mode == OBSERVE and not decision.allowed:
            would, decision = decision, Decision.allow()
        elif decision.held:
            decision = self._hold(tool, arguments, user, agent, channel, decision.reason or "")
        entry: dict[str, Any] = {"event": "call", "channel": channel, "tool": tool, "user": user, "arguments": arguments,
                                 "verdict": decision.verdict, "reason": (would or decision).reason,
                                 "approval": decision.approval_id}
        if would is not None:
            entry["would"] = would.verdict
        if agent is not None:
            entry["agent"] = agent
        if stopped is not None:
            entry["stopped"] = True
        self.journal.record(entry)
        return decision

    def _stopped(self, tool: str, agent: str | None, by_ai: bool) -> Decision | None:
        if self.stops is None:
            return None
        try:
            stops = self.stops.all()
        except Exception:  # fail closed
            return Decision.deny("the stop state cannot be read")
        for s in stops:
            if s.matches(tool, agent, by_ai):
                return Decision.deny(s.describe())
        return None

    def _evaluate(self, tool: str, r: Rules, arguments: dict[str, Any], user: str | None, agent: str | None,
                  channel: str) -> Decision:
        for g in r.granted:
            if self.is_granted is None:
                return Decision.deny(f'permission "{g.permission}" cannot be checked (no is_granted)')
            if g.subject is not None and g.subject not in arguments:
                return Decision.deny(f'argument "{g.subject}" missing for the permission check')
            try:
                ok = bool(self.is_granted(g.permission, arguments.get(g.subject) if g.subject else None, user))
            except Exception:
                ok = False
            if not ok:
                return Decision.deny(g.message or f'permission "{g.permission}" not granted'
                                     + (f' for {g.subject} "{arguments[g.subject]}"' if g.subject else ""))
        if self.agents and channel == AI:
            if agent is None or agent not in self.agents:
                if not self.allow_unknown_agent:
                    return Decision.deny(f'no agent identified for this call to "{tool}"' if agent is None
                                         else f'agent "{agent}" is not declared')
            elif tool not in self.agents[agent] and "*" not in self.agents[agent]:
                return Decision.deny(f'agent "{agent}" may not call "{tool}"')
        if r.rate is not None:
            who = "*" if r.rate.per == PER_TOOL else (user or "anonymous")
            if not self.counter.consume(f"{tool}:{who}", r.rate.limit, r.rate.seconds):
                return Decision.deny(f"more than {r.rate.limit} calls in {_duration(r.rate.seconds)}")
        hold = None
        for lim in r.limits:
            v = arguments.get(lim.argument)
            if isinstance(v, bool) or not _numeric(v):
                return Decision.deny(f'"{lim.argument}" must be a number')
            f = float(v)
            if lim.min is not None and f < lim.min:
                return Decision.deny(f"{lim.argument} {_s(v)} is below the minimum of {_s(lim.min)}")
            if f > lim.max:
                why = f"{lim.argument} {_s(v)} is above the limit of {_s(lim.max)}"
                if lim.above == ABOVE_DENY:
                    return Decision.deny(why)
                hold = hold or why
        if hold is None and r.approval is not None:
            hold = r.approval.reason or f'every "{tool}" is approved by a human'
        return Decision.pending(hold) if hold is not None else Decision.allow()

    def _hold(self, tool: str, arguments: dict[str, Any], user: str | None, agent: str | None, channel: str,
              reason: str) -> Decision:
        mine = [a for a in self.approvals.all(PENDING) if a.requested_by == user and not a.expired(self.expire_after)]
        if len(mine) >= self.max_pending_per_user:
            return Decision.deny(f"{len(mine)} requests already wait for a human")
        a = Approval.new(tool, _jsonable(arguments), user, reason, channel, agent)
        self.approvals.save(a)
        return Decision.pending(reason, a.id)

    # ---- the human side -----------------------------------------------------------------------

    def approve(self, approval_id: str, by: str) -> Approval:
        """A human approves: the tool runs now, with the arguments it was asked with."""
        a = self._waiting(approval_id, by)
        fn = self._tools.get(a.tool)
        if fn is None:
            raise LookupError(f'tool "{a.tool}" is not registered with this guard')
        target = getattr(fn, "__wrapped__", fn)
        try:
            result = target(**a.arguments)
            a.status, a.result = APPROVED, None if result is None else str(result)[:2000]
        except Exception as e:  # the approval stands, the run failed: said, not hidden
            a.status, a.result = FAILED, f"{type(e).__name__}: {e}"[:2000]
        a.decided_by, a.decided_at = by, now_iso()
        self.approvals.save(a)
        self.journal.record({"event": "approval", "tool": a.tool, "approval": a.id, "status": a.status, "by": by})
        return a

    def reject(self, approval_id: str, by: str) -> Approval:
        a = self._waiting(approval_id, by)
        a.status, a.decided_by, a.decided_at = REJECTED, by, now_iso()
        self.approvals.save(a)
        self.journal.record({"event": "approval", "tool": a.tool, "approval": a.id, "status": a.status, "by": by})
        return a

    def _waiting(self, approval_id: str, by: str) -> Approval:
        a = self.approvals.get(approval_id)
        if a is None:
            raise LookupError(f"no request {approval_id}")
        if a.status != PENDING:
            raise ValueError(f"request {approval_id} is already {a.status}")
        if a.expired(self.expire_after):
            raise ValueError(f"request {approval_id} has expired")
        if self.four_eyes and by == a.requested_by:
            raise PermissionError("four eyes: the person who asked cannot approve their own request")
        return a

    def stop(self, scope: str = "all", target: str | None = None, *, by: str | None = None, reason: str | None = None) -> None:
        if self.stops is None:
            raise RuntimeError("no stop store configured")
        if scope not in ("all", "agents", "tool", "agent") or ((scope in ("tool", "agent")) != bool(target)):
            raise ValueError("stop: scope all|agents (no target) or tool|agent (with a target)")
        self.stops.stop(Stop(scope, target, by, reason))
        self.journal.record({"event": "stop", "scope": scope, "target": target, "by": by, "reason": reason})

    def resume(self, scope: str = "all", target: str | None = None, *, by: str | None = None) -> bool:
        if self.stops is None:
            return False
        done = self.stops.resume(scope, target)
        if done:
            self.journal.record({"event": "resume", "scope": scope, "target": target, "by": by})
        return done

    def rules(self) -> dict[str, list[str]]:
        return {name: rules_of(fn).describe() for name, fn in sorted(self._tools.items())}


def _numeric(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _s(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(v)


def _duration(s: int) -> str:
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s % n == 0 and s >= n:
            k = s // n
            return f"{k} {unit}{'s' if k > 1 else ''}"
    return f"{s} seconds"


def _jsonable(arguments: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in arguments.items():
        out[k] = v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
    return out
