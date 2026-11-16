"""The rules, written on the tool itself. Each decorator only records a rule; Guard reads and enforces them.

    @limit("amount", max=50, above=APPROVAL, min=0)
    @rate_limit(5, per_seconds=60)
    def refund(order: str, amount: float) -> str: ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

APPROVAL = "approval"
DENY = "deny"
PER_USER = "user"
PER_TOOL = "tool"

ATTR = "__tool_guard_rules__"


@dataclass(frozen=True)
class Granted:
    permission: str
    subject: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class Limit:
    argument: str
    max: float
    above: str = DENY
    min: float | None = None


@dataclass(frozen=True)
class AllowedValues:
    argument: str
    patterns: tuple[str, ...]
    otherwise: str = APPROVAL


@dataclass(frozen=True)
class RateLimit:
    limit: int
    seconds: int
    per: str = PER_USER


@dataclass(frozen=True)
class RequiresApproval:
    reason: str | None = None


@dataclass
class Rules:
    granted: list[Granted] = field(default_factory=list)
    limits: list[Limit] = field(default_factory=list)
    values: list[AllowedValues] = field(default_factory=list)
    rate: RateLimit | None = None
    approval: RequiresApproval | None = None

    def empty(self) -> bool:
        return not (self.granted or self.limits or self.values or self.rate or self.approval)

    def describe(self) -> list[str]:
        out = [f"granted {g.permission}" + (f" on {g.subject}" if g.subject else "") for g in self.granted]
        if self.rate:
            out.append(f"rate_limit {self.rate.limit} per {self.rate.seconds}s, per {self.rate.per}")
        for lim in self.limits:
            low = f"{_num(lim.min)} ≤ " if lim.min is not None else ""
            out.append(f"limit {low}{lim.argument} ≤ {_num(lim.max)}, above: "
                       + ("refused" if lim.above == DENY else "held for a human"))
        for v in self.values:
            out.append(f"allowed_values {v.argument} in {', '.join(v.patterns)}, otherwise: "
                       + ("refused" if v.otherwise == DENY else "held for a human"))
        if self.approval:
            out.append("requires_approval" + (f" {self.approval.reason}" if self.approval.reason else ""))
        return out


def rules_of(fn: Callable[..., Any]) -> Rules:
    while True:
        r = getattr(fn, ATTR, None)
        if r is not None:
            return r
        inner = getattr(fn, "__wrapped__", None)
        if inner is None:
            return Rules()
        fn = inner


def _rules(fn: Callable[..., Any]) -> Rules:
    r = fn.__dict__.get(ATTR)
    if r is None:
        r = Rules()
        setattr(fn, ATTR, r)
    return r


def granted(permission: str, subject: str | None = None, message: str | None = None):
    """The current user must hold `permission` (on the argument named `subject`, if given)."""
    def deco(fn):
        _rules(fn).granted.append(Granted(permission, subject, message))
        return fn
    return deco


def limit(argument: str, *, max: float, above: str = DENY, min: float | None = None):
    """A numeric argument stays within bounds. Above `max`: refused (DENY) or held for a human (APPROVAL)."""
    if above not in (DENY, APPROVAL):
        raise ValueError('above must be "deny" or "approval"')

    def deco(fn):
        _rules(fn).limits.append(Limit(argument, float(max), above, None if min is None else float(min)))
        return fn
    return deco


def allowed_values(argument: str, *patterns: str, otherwise: str = APPROVAL):
    """A text argument (or every item of a list) must match one of the patterns, shell-style and case-insensitive:
    allowed_values("to", "*@mycompany.com"). Otherwise: held for a human (APPROVAL, default) or refused (DENY)."""
    if not patterns:
        raise ValueError("allowed_values needs at least one pattern")
    if otherwise not in (DENY, APPROVAL):
        raise ValueError('otherwise must be "deny" or "approval"')

    def deco(fn):
        _rules(fn).values.append(AllowedValues(argument, tuple(patterns), otherwise))
        return fn
    return deco


def rate_limit(limit: int, *, per_seconds: int, per: str = PER_USER):
    """At most `limit` calls in `per_seconds`, counted per user (default) or for the whole tool."""
    if limit < 1 or per_seconds < 1 or per not in (PER_USER, PER_TOOL):
        raise ValueError("rate_limit needs limit ≥ 1, per_seconds ≥ 1, per in (user, tool)")

    def deco(fn):
        _rules(fn).rate = RateLimit(limit, per_seconds, per)
        return fn
    return deco


def requires_approval(reason: str | None = None):
    """Every call waits for a human."""
    def deco(fn):
        _rules(fn).approval = RequiresApproval(reason)
        return fn
    return deco


def _num(v: float | None) -> str:
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else str(v)
