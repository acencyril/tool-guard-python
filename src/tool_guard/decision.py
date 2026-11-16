"""What Tool Guard decided about one call, and the message the model reads when it was blocked."""

from __future__ import annotations

from dataclasses import dataclass

ALLOW = "allow"
DENY = "deny"
PENDING = "pending"


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str | None = None
    approval_id: str | None = None

    @classmethod
    def allow(cls) -> "Decision":
        return cls(ALLOW)

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(DENY, reason)

    @classmethod
    def pending(cls, reason: str, approval_id: str = "") -> "Decision":
        return cls(PENDING, reason, approval_id)

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    @property
    def held(self) -> bool:
        return self.verdict == PENDING

    def message_for_model(self) -> str:
        """Same wording as the Symfony bundle: the model is told plainly not to retry nor work around it."""
        if self.verdict == DENY:
            return (f"Refused by policy: {self.reason}. Do not retry this action and do not try another way "
                    "to do it; tell the user it is not possible.")
        if self.verdict == PENDING:
            return (f"This action needs a human approval ({self.reason}). Request {self.approval_id} has been sent "
                    "to the team; it is NOT done yet. Do not retry; tell the user it is pending review.")
        return ""


class ToolCallBlocked(Exception):
    """Raised by Guard.call() when a call is refused or held: the tool did not run."""

    def __init__(self, decision: Decision, tool: str):
        super().__init__(decision.message_for_model())
        self.decision = decision
        self.tool = tool

    @property
    def held(self) -> bool:
        return self.decision.held
