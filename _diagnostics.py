from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ActionFailureInfo:
    action: str
    error_type: str
    message: str
    url: str
    selector: str = ""
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def summary(self) -> str:
        parts = [self.action, self.error_type]
        if self.selector:
            parts.append(self.selector)
        if self.message:
            parts.append(self.message)
        return " | ".join(part for part in parts if part)

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "error_type": self.error_type,
            "message": self.message,
            "url": self.url,
            "selector": self.selector,
            "occurred_at": self.occurred_at,
        }


class UIActionError(RuntimeError):
    def __init__(self, failure: ActionFailureInfo):
        super().__init__(failure.summary)
        self.failure = failure
