from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from typing import Any, TypeVar

from .adapter import ImagePathInput, XTextAdapter
from .base import (
    AccountStats,
    ActionPreflight,
    ActionResult,
    ControllerHealth,
    LoginState,
    MediaCaptureData,
    ObservedNotificationData,
    ObservedPostData,
    TimelineReadResult,
)

T = TypeVar("T")


class SyncXController:
    """Synchronous facade for XController's async browser service APIs."""

    def __init__(
        self,
        profile_path: str | None = None,
        settings: Any | None = None,
        proxy: str | None = None,
        *,
        adapter: XTextAdapter | None = None,
    ) -> None:
        if adapter is None and profile_path is None:
            raise ValueError("profile_path is required when adapter is not provided")
        self._adapter = adapter or XTextAdapter(profile_path=str(profile_path), settings=settings, proxy=proxy)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run_loop, name="xcontroller-sync-loop", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if not self._loop.is_running():
            raise RuntimeError("sync_controller_loop_not_started")

    @property
    def adapter(self) -> XTextAdapter:
        return self._adapter

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def _call(self, awaitable: Awaitable[T]) -> T:
        if self._closed:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise RuntimeError("sync_controller_closed")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result()

    def __enter__(self) -> SyncXController:
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def start(self) -> None:
        return self._call(self._adapter.start())

    def close(self) -> None:
        if self._closed:
            return None
        close_error: BaseException | None = None
        try:
            self._call(self._adapter.close())
        except BaseException as exc:
            close_error = exc
        finally:
            self._closed = True
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if not self._loop.is_closed():
                self._loop.close()
        if close_error:
            raise close_error
        return None

    def is_logged_in(self) -> bool:
        return self._call(self._adapter.is_logged_in())

    def open_login_page(self) -> None:
        return self._call(self._adapter.open_login_page())

    def login_state(self) -> LoginState:
        return self._call(self._adapter.login_state())

    def current_state(self) -> dict[str, str]:
        return self._call(self._adapter.current_state())

    def current_surface(self) -> dict[str, str]:
        return self._call(self._adapter.current_surface())

    def health_check(self) -> ControllerHealth:
        return self._call(self._adapter.health_check())

    def return_home(self, force_refresh: bool = False) -> bool:
        return self._call(self._adapter.return_home(force_refresh=force_refresh))

    def settle_after_action(
        self,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> bool:
        return self._call(
            self._adapter.settle_after_action(
                tab=tab,
                force_refresh=force_refresh,
                reset_scroll=reset_scroll,
            )
        )

    def read_timeline_detailed(
        self,
        limit: int = 20,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> TimelineReadResult:
        return self._call(
            self._adapter.read_timeline_detailed(
                limit=limit,
                tab=tab,
                force_refresh=force_refresh,
                reset_scroll=reset_scroll,
            )
        )

    def read_notifications(
        self,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[ObservedNotificationData]:
        return self._call(self._adapter.read_notifications(limit=limit, unread_only=unread_only))

    def read_post_thread_context(
        self,
        post_id: str,
        limit: int = 6,
        include_parent: bool = True,
        include_target: bool = True,
        include_replies: bool = True,
    ) -> list[ObservedPostData]:
        return self._call(
            self._adapter.read_post_thread_context(
                post_id,
                limit=limit,
                include_parent=include_parent,
                include_target=include_target,
                include_replies=include_replies,
            )
        )

    def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        return self._call(self._adapter.search_posts(query, limit=limit))

    def preflight_action(
        self,
        platform_post_id: str,
        action: str = "reply",
        *,
        open_composer: bool = False,
    ) -> ActionPreflight:
        return self._call(
            self._adapter.preflight_action(
                platform_post_id,
                action=action,
                open_composer=open_composer,
            )
        )

    def view_post_detailed(
        self,
        platform_post_id: str,
        dwell_seconds: tuple[int, int] = (3, 8),
    ) -> ActionResult:
        return self._call(self._adapter.view_post_detailed(platform_post_id, dwell_seconds=dwell_seconds))

    def like_post_detailed(self, platform_post_id: str) -> ActionResult:
        return self._call(self._adapter.like_post_detailed(platform_post_id))

    def reply_to_post_detailed(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        return self._call(self._adapter.reply_to_post_detailed(platform_post_id, text, image_paths=image_paths))

    def quote_post_detailed(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        return self._call(self._adapter.quote_post_detailed(platform_post_id, text=text, image_paths=image_paths))

    def repost_post_detailed(self, platform_post_id: str) -> ActionResult:
        return self._call(self._adapter.repost_post_detailed(platform_post_id))

    def follow_user_detailed(self, username: str) -> ActionResult:
        return self._call(self._adapter.follow_user_detailed(username))

    def post_text_detailed(self, text: str, image_paths: ImagePathInput | None = None) -> ActionResult:
        return self._call(self._adapter.post_text_detailed(text, image_paths=image_paths))

    def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        return self._call(self._adapter.post_metrics(platform_post_id))

    def profile_recent_metrics(self, username: str, limit: int = 40) -> list[dict[str, int | str]]:
        return self._call(self._adapter.profile_recent_metrics(username, limit=limit))

    def account_stats(self, handle: str | None = None) -> AccountStats:
        return self._call(self._adapter.account_stats(handle))

    def debug_snapshot(self, output_dir: str, article_limit: int = 12) -> dict[str, Any]:
        return self._call(self._adapter.debug_snapshot(output_dir, article_limit=article_limit))

    def capture_post_media(
        self,
        platform_post_id: str,
        output_dir: str,
        frame_count: int = 3,
    ) -> list[MediaCaptureData]:
        return self._call(
            self._adapter.capture_post_media(
                platform_post_id,
                output_dir,
                frame_count=frame_count,
            )
        )


XControllerService = SyncXController
