"""Tool Guard for Python: permissions, limits, rate limits, human approval and a stop button for the
tools an AI agent may call. A prompt asks; Tool Guard decides, before the tool runs."""

from .decision import Decision, ToolCallBlocked
from .guard import AI, CODE, ENFORCE, OBSERVE, Guard
from .rules import APPROVAL, DENY, PER_TOOL, PER_USER, granted, limit, rate_limit, requires_approval, rules_of
from .stores import (Approval, FileApprovals, FileCounter, FileJournal, FileStops, MemoryApprovals, MemoryCounter,
                     MemoryJournal, MemoryStops, Stop)

__version__ = "0.1.0"

__all__ = [
    "Guard", "Decision", "ToolCallBlocked", "granted", "limit", "rate_limit", "requires_approval", "rules_of",
    "APPROVAL", "DENY", "PER_USER", "PER_TOOL", "ENFORCE", "OBSERVE", "AI", "CODE",
    "Approval", "Stop", "FileApprovals", "FileJournal", "FileStops", "FileCounter",
    "MemoryApprovals", "MemoryJournal", "MemoryStops", "MemoryCounter",
]
