from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class ControllerSettings:
    """Runtime knobs for browser behavior and human-like interaction timing."""

    strict_ui_failures: bool = False

    anti_bot_typing_min_ms: int = 40
    anti_bot_typing_max_ms: int = 260
    anti_bot_pause_min_ms: int = 250
    anti_bot_pause_max_ms: int = 1200
    anti_bot_mouse_move_ms: int = 120
    human_scroll_min_ms: int = 500
    human_scroll_max_ms: int = 2500

    browser_width_min: int = 1280
    browser_width_max: int = 1780
    browser_height_min: int = 820
    browser_height_max: int = 1120

    default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    @classmethod
    def from_any(cls, value: Any | None) -> "ControllerSettings":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        data: dict[str, Any] = {}
        for key in cls.__dataclass_fields__.keys():
            if isinstance(value, Mapping) and key in value:
                data[key] = value[key]
            elif hasattr(value, key):
                data[key] = getattr(value, key)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in self.__dataclass_fields__.keys()
        }
