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


class ActionFailureStage:
    """Stable stage taxonomy for `ActionResult.failure_stage` values."""

    NOT_STARTED = "not_started"
    TARGET_LOOKUP = "target_lookup"
    MEDIA_ATTACH = "media_attach"
    TEXT_ENTRY = "text_entry"
    SUBMIT_LOOKUP = "submit_lookup"
    CONFIRMATION = "confirmation"
    POST_SUBMIT = "post_submit"
    COMPOSER_OPEN = "composer_open"
    ACTION_CONTROL = "action_control"
    PREFLIGHT = "preflight"
    UNKNOWN = "unknown"
