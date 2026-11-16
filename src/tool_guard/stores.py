"""Where Tool Guard keeps its state: waiting approvals, the journal, the stops, the rate counters.

The file stores work across processes (one JSON file per approval, a JSONL journal, a locked JSON file for
stops and counters). The memory stores are for tests and single-process scripts.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows: one process at a time
    fcntl = None  # type: ignore[assignment]

PENDING, APPROVED, REJECTED, FAILED, EXPIRED = "pending", "approved", "rejected", "failed", "expired"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Approval:
    id: str
    tool: str
    arguments: dict[str, Any]
    requested_by: str | None
    reason: str
    requested_at: str
    channel: str = "ai"
    agent: str | None = None
    status: str = PENDING
    decided_by: str | None = None
    decided_at: str | None = None
    result: str | None = None
    queued: bool = False

    @classmethod
    def new(cls, tool: str, arguments: dict[str, Any], user: str | None, reason: str, channel: str, agent: str | None) -> "Approval":
        return cls(secrets.token_hex(4), tool, arguments, user, reason, now_iso(), channel, agent)

    def expired(self, seconds: int | None, at: float | None = None) -> bool:
        if self.status != PENDING or seconds is None:
            return False
        asked = datetime.fromisoformat(self.requested_at).timestamp()
        return (at if at is not None else time.time()) - asked >= seconds


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tg-")
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    os.replace(tmp, path)


class MemoryApprovals:
    def __init__(self) -> None:
        self._items: dict[str, Approval] = {}
        self._lock = threading.Lock()

    def save(self, a: Approval) -> None:
        with self._lock:
            self._items[a.id] = a

    def get(self, approval_id: str) -> Approval | None:
        return self._items.get(approval_id)

    def all(self, status: str | None = None) -> list[Approval]:
        items = sorted(self._items.values(), key=lambda a: a.requested_at, reverse=True)
        return [a for a in items if status is None or a.status == status]


class FileApprovals:
    """One JSON file per request in `directory`."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)

    def save(self, a: Approval) -> None:
        if not all(c in "0123456789abcdef" for c in a.id):
            raise ValueError("bad approval id")
        _write_atomic(self.dir / f"{a.id}.json", json.dumps(asdict(a), ensure_ascii=False))

    def get(self, approval_id: str) -> Approval | None:
        if not approval_id or not all(c in "0123456789abcdef" for c in approval_id):
            return None
        p = self.dir / f"{approval_id}.json"
        return Approval(**json.loads(p.read_text())) if p.exists() else None

    def all(self, status: str | None = None) -> list[Approval]:
        out = []
        for p in self.dir.glob("*.json") if self.dir.exists() else []:
            try:
                out.append(Approval(**json.loads(p.read_text())))
            except (ValueError, TypeError):
                continue
        out.sort(key=lambda a: a.requested_at, reverse=True)
        return [a for a in out if status is None or a.status == status]


class MemoryJournal:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append({"at": now_iso(), **entry})


class FileJournal:
    """One JSON line per decision: who asked, which tool, which arguments, what Tool Guard decided and why."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def record(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": now_iso(), **entry}, ensure_ascii=False, default=str)
        with _locked(self.path), open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


@dataclass
class Stop:
    """The stop button: everything, every AI agent, one tool, or one agent."""

    scope: str  # all | agents | tool | agent
    target: str | None = None
    by: str | None = None
    reason: str | None = None
    at: str = field(default_factory=now_iso)

    def matches(self, tool: str, agent: str | None, by_ai: bool) -> bool:
        return (self.scope == "all" or (self.scope == "agents" and by_ai)
                or (self.scope == "tool" and self.target == tool) or (self.scope == "agent" and agent is not None and self.target == agent))

    def describe(self) -> str:
        what = {"all": "every tool is stopped", "agents": "AI agents are stopped",
                "tool": f'tool "{self.target}" is stopped', "agent": f'agent "{self.target}" is stopped'}[self.scope]
        return what + (f" ({self.reason})" if self.reason else "")


class FileStops:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def all(self) -> list[Stop]:
        if not self.path.exists():
            return []
        return [Stop(**s) for s in json.loads(self.path.read_text() or "[]")]

    def stop(self, s: Stop) -> None:
        with _locked(self.path):
            items = [x for x in self.all() if (x.scope, x.target) != (s.scope, s.target)] + [s]
            _write_atomic(self.path, json.dumps([asdict(x) for x in items], ensure_ascii=False))

    def resume(self, scope: str, target: str | None = None) -> bool:
        with _locked(self.path):
            items = self.all()
            kept = [x for x in items if (x.scope, x.target) != (scope, target)]
            _write_atomic(self.path, json.dumps([asdict(x) for x in kept], ensure_ascii=False))
            return len(kept) != len(items)


class MemoryStops(FileStops):
    def __init__(self) -> None:
        self._items: list[Stop] = []

    def all(self) -> list[Stop]:
        return list(self._items)

    def stop(self, s: Stop) -> None:
        self._items = [x for x in self._items if (x.scope, x.target) != (s.scope, s.target)] + [s]

    def resume(self, scope: str, target: str | None = None) -> bool:
        before = len(self._items)
        self._items = [x for x in self._items if (x.scope, x.target) != (scope, target)]
        return len(self._items) != before


class MemoryCounter:
    """Fixed-window counter: the window starts at the first call."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, limit: int, seconds: int, at: float | None = None) -> bool:
        t = at if at is not None else time.time()
        with self._lock:
            start, n = self._windows.get(key, (t, 0))
            if t - start >= seconds:
                start, n = t, 0
            if n >= limit:
                return False
            self._windows[key] = (start, n + 1)
            return True


class FileCounter(MemoryCounter):
    """The same counter shared by every process, in one locked JSON file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self.path = Path(path)

    def consume(self, key: str, limit: int, seconds: int, at: float | None = None) -> bool:
        t = at if at is not None else time.time()
        with _locked(self.path):
            data = json.loads(self.path.read_text() or "{}") if self.path.exists() else {}
            start, n = data.get(key, [t, 0])
            if t - start >= seconds:
                start, n = t, 0
            if n >= limit:
                return False
            data[key] = [start, n + 1]
            data = {k: v for k, v in data.items() if t - v[0] < 86400}
            _write_atomic(self.path, json.dumps(data))
            return True
