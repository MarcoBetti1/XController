from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ObservedPostData:
    """Normalized post payload returned by read/search operations."""

    platform_post_id: str
    author: str
    text: str
    raw: dict[str, Any]

    @property
    def metrics(self) -> dict[str, Any]:
        value = self.raw.get("metrics")
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_post_id": self.platform_post_id,
            "author": self.author,
            "text": self.text,
            "raw": dict(self.raw),
        }


class SocialPlatformAdapter(ABC):
    """Base async interface for click-driven social platform automation."""

    platform: str = "base"

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def is_logged_in(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def open_login_page(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def current_state(self) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    async def recover_home(self, force_nav: bool = False) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def refresh_home(self, force_nav: bool = False) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def profile_recent_metrics(self, username: str, limit: int = 40) -> list[dict[str, int | str]]:
        raise NotImplementedError

    @abstractmethod
    async def read_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        raise NotImplementedError

    @abstractmethod
    async def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        raise NotImplementedError

    @abstractmethod
    async def post_text(self, text: str, image_paths: Any | None = None) -> str | None:
        raise NotImplementedError

    async def post_image(self, image_paths: Any, text: str = "") -> str | None:
        raise NotImplementedError

    async def quote_post(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: Any | None = None,
    ) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def like_post(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    async def comment_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: Any | None = None,
    ) -> str | None:
        return await self.reply_to_post(platform_post_id, text, image_paths=image_paths)

    @abstractmethod
    async def reply_to_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: Any | None = None,
    ) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def follow_user(self, username: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def unfollow_user(self, username: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    async def delete_post(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_reply(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_repost(self, platform_post_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_posts(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_replies(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_reposts(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def delete_all_content(self) -> dict[str, list[str]]:
        raise NotImplementedError
