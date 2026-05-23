from __future__ import annotations

import logging
import os
import random
import re
import time
import contextlib
import hashlib
import warnings
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse
from typing import Any, Callable, Sequence

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.sync_api import BrowserContext as SyncBrowserContext, Page as SyncPage, sync_playwright as sync_playwright

from . import _ui_selectors as ui
from ._diagnostics import ActionFailureInfo, UIActionError
from .base import (
    AccountStats,
    ActionPreflight,
    ActionResult,
    ControllerHealth,
    LoginState,
    MediaCaptureData,
    MediaPreflight,
    ObservedMediaData,
    ObservedNotificationData,
    ObservedPostData,
    SocialPlatformAdapter,
    TimelineReadResult,
)
from .human import HumanMotion
from .settings import ControllerSettings

logger = logging.getLogger("XController.adapter")

ImagePath = str | os.PathLike[str]
ImagePathInput = ImagePath | Sequence[ImagePath]


class XTextAdapter(SocialPlatformAdapter):
    """Click-first Playwright controller for automating common X workflows."""

    platform = "x"
    BASE_URL = "https://x.com"
    NAV_RETRIES = 3
    POST_ID_RE = re.compile(r"([0-9]+)")
    STATUS_URL_RE = re.compile(r"/status/([0-9]+)")
    PROFILE_URL_RE = re.compile(r"https?://(?:www\.)?x\.com/([^/?#]+)$")
    HANDLE_TEXT_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
    ACCOUNT_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
    PROFILE_TITLE_RE = re.compile(r"^(.*?)\s+\(@([A-Za-z0-9_]{1,15})\)")
    PROGRAMMER_ERROR_TYPES = (AssertionError, AttributeError, KeyError, TypeError)

    def __init__(self, profile_path: str, settings: Any | None = None, proxy: str | None = None):
        self.settings = ControllerSettings.from_any(settings)
        profile_dir = Path(profile_path).expanduser()
        if not profile_dir.is_absolute():
            profile_dir = (Path.cwd() / profile_dir).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.profile_path = str(profile_dir)
        self.proxy = proxy
        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._sync_playwright = None
        self._sync_context: SyncBrowserContext | None = None
        self._sync_page: SyncPage | None = None
        self._sync_mode = False
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="social-agent-playwright",
        )
        self._authenticated_handle: str | None = None
        self.last_action_error: ActionFailureInfo | None = None
        self.human = HumanMotion(self.settings)

    async def start(self) -> None:
        if self.context:
            return
        if self._sync_page:
            self.context = self._sync_context  # type: ignore[assignment]
            self.page = self._sync_page  # type: ignore[assignment]
            return

        start_retries = 3
        for attempt in range(1, start_retries + 1):
            try:
                if self._prefer_sync_playwright():
                    await self._start_sync_fallback()
                    if self._sync_page is None:
                        raise RuntimeError("playwright_sync_fallback_not_initialized")
                    self.context = self._sync_context  # type: ignore[assignment]
                    self.page = self._sync_page  # type: ignore[assignment]
                    await self._wait_network_idle(1800)
                    return

                try:
                    self.playwright = await async_playwright().start()
                except NotImplementedError as exc:
                    logger.warning(
                        "Async Playwright start unavailable in this event loop; falling back to sync driver. %s",
                        exc,
                    )
                    await self._start_sync_fallback()
                else:
                    viewport = {
                        "width": random.randint(self.settings.browser_width_min, self.settings.browser_width_max),
                        "height": random.randint(self.settings.browser_height_min, self.settings.browser_height_max),
                    }
                    context_kwargs: dict[str, Any] = {
                        "headless": bool(self.settings.headless),
                        "viewport": viewport,
                        "user_agent": self.settings.default_user_agent,
                        "args": ["--disable-blink-features=AutomationControlled"],
                    }
                    if self.proxy:
                        context_kwargs["proxy"] = {"server": self.proxy}
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=self.profile_path,
                        **context_kwargs,
                    )
                    self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                    self._sync_mode = False
                    await self._wait_network_idle(1800)
                    return

                if self._sync_page is None:
                    raise RuntimeError("playwright_sync_fallback_not_initialized")
                self.context = self._sync_context  # type: ignore[assignment]
                self.page = self._sync_page  # type: ignore[assignment]
                await self._wait_network_idle(1800)
                return
            except Exception as exc:
                transient = self._is_target_closed_error(exc) or self._is_driver_connection_closed(exc)
                profile_issue = self._is_profile_in_use_error(exc)
                await self._hard_reset_browser_state()
                if attempt < start_retries and transient and not profile_issue:
                    logger.warning(
                        "Playwright start failed (attempt %s/%s). Retrying in %ss. error=%s",
                        attempt,
                        start_retries,
                        attempt,
                        str(exc)[:280],
                    )
                    await asyncio.sleep(float(attempt))
                    continue
                if profile_issue:
                    raise RuntimeError("profile_in_use") from exc
                if self._is_driver_connection_closed(exc):
                    raise RuntimeError("playwright_driver_connection_closed") from exc
                if self._is_target_closed_error(exc):
                    raise RuntimeError("target_page_or_context_closed") from exc
                raise

    def _prefer_sync_playwright(self) -> bool:
        if self.settings.prefer_sync_playwright is not None:
            return bool(self.settings.prefer_sync_playwright)
        mode = str(self.settings.playwright_mode or "auto").strip().lower()
        if mode == "sync":
            return True
        if mode == "async":
            return False
        # Windows asyncio subprocess handling is frequently unavailable in this runtime.
        # Prefer sync Playwright there to avoid repeated transport failures.
        return os.name == "nt"

    async def _start_sync_fallback(self) -> None:
        viewport = {
            "width": random.randint(self.settings.browser_width_min, self.settings.browser_width_max),
            "height": random.randint(self.settings.browser_height_min, self.settings.browser_height_max),
        }
        context_kwargs: dict[str, Any] = {
            "headless": bool(self.settings.headless),
            "viewport": viewport,
            "user_agent": self.settings.default_user_agent,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.proxy:
            context_kwargs["proxy"] = {"server": self.proxy}
        await self._run_sync(self._start_sync_browser, context_kwargs)
        self._sync_mode = True

    async def _hard_reset_browser_state(self) -> None:
        self.context = None
        self.page = None
        self._sync_mode = False
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self._sync_playwright or self._sync_context or self._sync_page:
            try:
                await self._run_sync(self._close_sync_browser)
            except Exception:
                try:
                    self._close_sync_browser()
                except Exception:
                    pass

    def _start_sync_browser(self, context_kwargs: dict[str, Any]) -> None:
        if self._sync_playwright:
            try:
                if self._sync_context:
                    self._sync_context.close()
                self._sync_playwright.stop()
            except Exception:
                pass
            self._sync_playwright = None
            self._sync_context = None
            self._sync_page = None
        logger.info("Starting Playwright sync fallback for profile_path=%s", self.profile_path)
        self._sync_playwright = sync_playwright().start()
        self._sync_context = self._sync_playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            **context_kwargs,
        )
        self._sync_page = self._sync_context.pages[0] if self._sync_context.pages else self._sync_context.new_page()

    async def _run_sync(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="social-agent-playwright")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, partial(func, *args, **kwargs))
        except Exception as exc:
            if self._is_profile_in_use_error(exc):
                raise RuntimeError("profile_in_use") from exc
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            raise

    def _is_target_closed_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "target page, context or browser has been closed" in text
            or "targetclosederror" in text
            or "has been closed" in text and "page" in text
        )

    def _is_driver_connection_closed(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "connection closed while reading from the driver" in text
            or "connection closed while reading from driver" in text
            or "playwright_driver_connection_closed" in text
            or "driver process exited" in text
        )

    def _is_profile_in_use_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "profile appears to be in use" in text
            or "user data directory is already in use" in text
            or "process singleton" in text
            or "profile_in_use" in text
        )

    def _clear_action_error(self) -> None:
        self.last_action_error = None

    def _record_action_error(self, action: str, exc: Exception, *, selector: str = "") -> None:
        failure = ActionFailureInfo(
            action=action,
            error_type=type(exc).__name__,
            message=str(exc)[:260],
            url=self.page.url if self.page else "",
            selector=selector,
        )
        self.last_action_error = failure
        logger.warning(
            "ui_action_failed action=%s type=%s url=%s selector=%s error=%s",
            failure.action,
            failure.error_type,
            failure.url,
            failure.selector or "-",
            failure.message,
        )
        if self.settings.strict_ui_failures:
            raise UIActionError(failure) from exc

    def _handle_soft_ui_error(self, action: str, exc: Exception, *, selector: str = "") -> None:
        if isinstance(exc, self.PROGRAMMER_ERROR_TYPES):
            raise
        if self._is_driver_connection_closed(exc):
            raise RuntimeError("playwright_driver_connection_closed") from exc
        if self._is_target_closed_error(exc):
            raise RuntimeError("target_page_or_context_closed") from exc
        self._record_action_error(action, exc, selector=selector)

    async def _current_state_name(self) -> str:
        try:
            return str((await self.current_state()).get("state") or "")
        except Exception:
            return "unknown"

    async def _active_home_tab(self) -> str:
        if not self.page:
            return ""
        try:
            value = await self._evaluate(
                """() => {
                    const normalize = text => String(text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                    const tabs = Array.from(document.querySelectorAll('[role="tab"], a[aria-current="page"]'));
                    for (const tab of tabs) {
                        const text = normalize(tab.innerText || tab.textContent || tab.getAttribute('aria-label'));
                        const current = tab.getAttribute('aria-selected') === 'true' || tab.getAttribute('aria-current') === 'page';
                        if (!current) continue;
                        if (text.includes('following')) return 'following';
                        if (text.includes('for you')) return 'for_you';
                    }
                    return '';
                }"""
            )
            if value in {"for_you", "following"}:
                return str(value)
        except Exception:
            pass
        return ""

    async def current_surface(self) -> dict[str, str]:
        state = await self.current_state()
        state_name = str(state.get("state") or "")
        url = str(state.get("url") or "")
        active_home_tab = await self._active_home_tab()
        return {
            "state": state_name,
            "current_state": state_name,
            "url": url,
            "active_home_tab": active_home_tab,
            "active_tab": active_home_tab,
        }

    def sync(self):
        from .sync import SyncXController

        return SyncXController(adapter=self)

    async def _fill_action_context(self, result: ActionResult) -> ActionResult:
        result.current_url = self.page.url if self.page else ""
        result.current_state = await self._current_state_name()
        result.active_home_tab = await self._active_home_tab()
        if self.last_action_error and not result.diagnostic:
            result.diagnostic = {"last_action_error": self.last_action_error.to_dict()}
        return result

    async def _action_result(
        self,
        action: str,
        *,
        ok: bool = False,
        target_post_id: str = "",
        created_post_id: str = "",
        failure_reason: str = "",
        failure_stage: str = "unknown",
        attempts: int = 0,
        media_paths: list[str] | None = None,
        raw: dict[str, Any] | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> ActionResult:
        post_id = self._normalize_post_id(target_post_id)
        result = ActionResult(
            ok=ok,
            action=action,
            target_post_id=post_id,
            created_post_id=created_post_id,
            target_url=f"{self.BASE_URL}/i/web/status/{post_id}" if post_id else "",
            failure_reason=failure_reason,
            failure_stage=failure_stage,
            attempts=attempts,
            media_paths=list(media_paths or []),
            diagnostic=dict(diagnostic or {}),
            raw=dict(raw or {}),
        )
        return await self._fill_action_context(result)

    async def _screenshot(self, path: str) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._sync_page.screenshot, path=path, full_page=True)
            return
        await self.page.screenshot(path=path, full_page=True)

    async def _wait_network_idle(self, ms: int) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._wait_network_idle_sync, self._sync_page, ms)
            return
        await self.human.wait_for_network_idle(self.page, ms=ms)

    def _wait_network_idle_sync(self, page: SyncPage | None, ms: int) -> None:
        if not page:
            return
        try:
            page.wait_for_load_state("networkidle", timeout=ms)
        except Exception:
            pass

    async def close(self) -> None:
        if self.context:
            if self._sync_mode:
                try:
                    await self._run_sync(self._close_sync_browser)
                except RuntimeError as exc:
                    if str(exc) not in {"target_page_or_context_closed", "playwright_driver_connection_closed", "profile_in_use"}:
                        raise
            else:
                await self.context.close()
                self.context = None
                self.page = None
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
                self._sync_mode = False
                self._shutdown_executor()
                return
            self.context = None
            self.page = None
            self._sync_mode = False
            self._shutdown_executor()
            return

        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        if self._sync_playwright:
            try:
                await self._run_sync(self._close_sync_browser)
            except RuntimeError as exc:
                if str(exc) not in {"target_page_or_context_closed", "playwright_driver_connection_closed", "profile_in_use"}:
                    raise
        self._shutdown_executor()

    def _close_sync_browser(self) -> None:
        if self._sync_context:
            try:
                self._sync_context.close()
            except Exception:
                pass
            self._sync_context = None
        if self._sync_playwright:
            try:
                self._sync_playwright.stop()
            except Exception:
                pass
            self._sync_playwright = None
        self._sync_page = None

        return

    def _shutdown_executor(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    async def _count_locator(self, locator: Any) -> int:
        if self._sync_mode:
            return await self._run_sync(locator.count)
        return await locator.count()

    async def _any_selector(self, selectors: list[str]) -> bool:
        if not self.page:
            return False
        for selector in selectors:
            locator = self.page.locator(selector)
            if await self._count_locator(locator):
                return True
        return False

    async def _get_attribute(self, locator: Any, attr: str) -> str | None:
        if self._sync_mode:
            return await self._run_sync(locator.get_attribute, attr)
        return await locator.get_attribute(attr)

    async def _inner_text(self, locator: Any, timeout_ms: int | None = None) -> str:
        if self._sync_mode:
            if timeout_ms is None:
                return await self._run_sync(locator.inner_text)
            return await self._run_sync(locator.inner_text, timeout=timeout_ms)
        if timeout_ms is None:
            return await locator.inner_text()
        return await locator.inner_text(timeout=timeout_ms)

    async def _click(self, locator: Any) -> None:
        if self._sync_mode:
            await self._run_sync(self._click_sync, locator)
            return
        try:
            await locator.click(timeout=3500)
        except Exception:
            await locator.evaluate("el => el.click()")

    def _click_sync(self, locator: Any) -> None:
        try:
            locator.click(timeout=3500)
        except Exception:
            locator.evaluate("el => el.click()")

    async def _click_force(self, locator: Any) -> None:
        if self._sync_mode:
            await self._run_sync(self._click_force_sync, locator)
            return
        try:
            await locator.click(timeout=2200, force=True)
            return
        except Exception:
            pass
        try:
            await locator.dispatch_event("click")
            return
        except Exception:
            pass
        await locator.evaluate(
            """el => {
                el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                el.click();
            }"""
        )

    def _click_force_sync(self, locator: Any) -> None:
        try:
            locator.click(timeout=2200, force=True)
            return
        except Exception:
            pass
        try:
            locator.dispatch_event("click")
            return
        except Exception:
            pass
        locator.evaluate(
            """el => {
                el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
                el.click();
            }"""
        )

    async def _type_text(self, element: Any, text: str) -> None:
        if not text:
            return
        if self._sync_mode:
            await self._run_sync(self._type_text_sync, element, text)
            return
        await self.human.type_like_human(element, text)

    def _type_text_sync(self, element: Any, text: str) -> None:
        # Keep sync mode operations inside a single worker thread.
        if not self._sync_page:
            return
        with contextlib.suppress(Exception):
            element.click(timeout=2200)
        for ch in text:
            self._sync_page.keyboard.type(ch)
            time.sleep(random.uniform(self.settings.anti_bot_typing_min_ms, self.settings.anti_bot_typing_max_ms) / 1000.0)
            if random.random() < 0.02:
                time.sleep(random.uniform(20, 220) / 1000.0)

    async def _set_input_files(self, locator: Any, files: list[str], timeout_ms: int = 6000) -> None:
        if self._sync_mode:
            await self._run_sync(locator.set_input_files, files, timeout=timeout_ms)
            return
        await locator.set_input_files(files, timeout=timeout_ms)

    async def _composer_scopes_for_textbox(self, textbox: Any | None, include_page: bool = True) -> list[Any]:
        scopes: list[Any] = []
        if textbox is not None:
            for xpath in (
                "ancestor::*[@role='dialog'][1]",
                "ancestor::*[self::form or self::article][1]",
            ):
                with contextlib.suppress(Exception):
                    scope = textbox.locator(f"xpath={xpath}").first
                    if await self._count_locator(scope):
                        scopes.append(scope)
        if include_page and self.page:
            scopes.append(self.page)
        return scopes

    async def _find_media_input_for_composer(self, textbox: Any | None, timeout_ms: int = 3000) -> Any | None:
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for scope in await self._composer_scopes_for_textbox(textbox):
                for selector in ui.MEDIA_INPUT_SELECTORS:
                    with contextlib.suppress(Exception):
                        locator = scope.locator(selector)
                        if await self._count_locator(locator):
                            return locator.first
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _wait_for_media_attachment(self, textbox: Any | None, timeout_seconds: float = 12.0) -> bool:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            scopes = await self._composer_scopes_for_textbox(textbox, include_page=False)
            if not scopes and self.page:
                scopes = [self.page]
            for scope in scopes:
                for selector in ui.MEDIA_PREVIEW_SELECTORS:
                    with contextlib.suppress(Exception):
                        if await self._count_locator(scope.locator(selector)):
                            return True
            await asyncio.sleep(0.25)
        return False

    async def _attach_images_to_composer(self, textbox: Any | None, image_paths: list[str]) -> bool:
        if not image_paths:
            return True
        media_input = await self._find_media_input_for_composer(textbox, timeout_ms=3500)
        if not media_input:
            logger.warning("media_file_input_not_found count=%s", len(image_paths))
            return False
        try:
            await self._set_input_files(media_input, image_paths)
            await self.human.jitter(650, 1500)
            if await self._wait_for_media_attachment(textbox):
                return True
            logger.warning("media_upload_preview_not_found count=%s", len(image_paths))
            return False
        except Exception as exc:
            logger.warning("media_upload_failed count=%s error=%s", len(image_paths), str(exc)[:260])
            return False

    async def _random_scroll(self, *steps: int) -> None:
        if self._sync_mode:
            await self._run_sync(self._random_scroll_sync, *steps)
            return
        if not self.page:
            return
        if not steps:
            steps = (random.randint(-500, 650),)
        for index, step in enumerate(steps):
            await self._smooth_scroll_async(step)
            if index < len(steps) - 1:
                await self.human.jitter(90, 340)

    def _random_scroll_sync(self, *steps: int) -> None:
        if not self._sync_page:
            return
        if not steps:
            steps = (random.randint(-500, 650),)
        for index, step in enumerate(steps):
            self._smooth_scroll_sync(step)
            if index < len(steps) - 1:
                time.sleep(random.uniform(0.08, 0.35))

    async def _smooth_scroll_async(self, delta: int) -> None:
        if not self.page:
            return
        direction = 1 if delta >= 0 else -1
        remaining = abs(int(delta))
        if remaining == 0:
            return
        chunks = max(2, min(10, (remaining // 140) + 1))
        for idx in range(chunks):
            if remaining <= 0:
                break
            tail = max(1, chunks - idx)
            low = 50
            high = min(260, max(low + 10, remaining // tail + 90))
            chunk = min(remaining, random.randint(low, high))
            await self.page.mouse.wheel(0, direction * chunk)
            remaining -= chunk
            await self.human.jitter(45, 170)
        if remaining > 0:
            await self.page.mouse.wheel(0, direction * remaining)
            await self.human.jitter(45, 140)

    def _smooth_scroll_sync(self, delta: int) -> None:
        if not self._sync_page:
            return
        direction = 1 if delta >= 0 else -1
        remaining = abs(int(delta))
        if remaining == 0:
            return
        chunks = max(2, min(10, (remaining // 140) + 1))
        for idx in range(chunks):
            if remaining <= 0:
                break
            tail = max(1, chunks - idx)
            low = 50
            high = min(260, max(low + 10, remaining // tail + 90))
            chunk = min(remaining, random.randint(low, high))
            self._sync_page.mouse.wheel(0, direction * chunk)
            remaining -= chunk
            time.sleep(random.uniform(0.04, 0.17))
        if remaining > 0:
            self._sync_page.mouse.wheel(0, direction * remaining)
            time.sleep(random.uniform(0.04, 0.14))

    async def _move_mouse(self) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._move_mouse_sync)
            return
        await self.human.move_mouse_random(self.page)

    def _move_mouse_sync(self) -> None:
        if not self._sync_page:
            return
        viewport = self._sync_page.viewport_size or {"width": 1365, "height": 768}
        x = random.randint(0, viewport["width"])
        y = random.randint(0, viewport["height"])
        self._sync_page.mouse.move(x, y, steps=self.settings.anti_bot_mouse_move_ms)
        time.sleep(random.uniform(20, 180) / 1000.0)

    async def _page_content(self) -> str:
        if not self.page:
            return ""
        if self._sync_mode:
            return await self._run_sync(self._page_content_sync, self._sync_page)
        return await self.page.content()

    def _page_content_sync(self, page: SyncPage | None) -> str:
        if not page:
            return ""
        try:
            return page.content()
        except Exception:
            return ""

    async def _evaluate(self, script: str) -> Any:
        if not self.page:
            return None
        if self._sync_mode:
            return await self._run_sync(self._sync_page.evaluate, script)
        return await self.page.evaluate(script)

    async def _new_context_page(self) -> Any | None:
        if self._sync_mode:
            if not self._sync_context:
                return None
            return await self._run_sync(self._sync_context.new_page)
        if not self.context:
            return None
        return await self.context.new_page()

    async def _close_context_page(self, page: Any | None) -> None:
        if not page:
            return
        try:
            if self._sync_mode:
                await self._run_sync(page.close)
            else:
                await page.close()
        except Exception:
            pass

    async def _page_url(self, page: Any | None) -> str:
        if not page:
            return ""
        try:
            if self._sync_mode:
                return str(await self._run_sync(lambda: getattr(page, "url", "") or ""))
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    async def _page_wait_network_idle(self, page: Any | None, ms: int = 1200) -> None:
        if not page:
            return
        try:
            if self._sync_mode:
                await self._run_sync(page.wait_for_load_state, "networkidle", timeout=ms)
            else:
                await page.wait_for_load_state("networkidle", timeout=ms)
        except Exception:
            pass

    async def _page_goto(self, page: Any, url: str) -> None:
        retries = max(1, int(self.NAV_RETRIES))
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if self._sync_mode:
                    await self._run_sync(page.goto, url, wait_until="domcontentloaded")
                else:
                    await page.goto(url, wait_until="domcontentloaded")
                await self.human.jitter(300, 900)
                await self._page_wait_network_idle(page, 1400)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "page_navigation_failed attempt=%s/%s url=%s error=%s",
                    attempt,
                    retries,
                    url,
                    str(exc)[:300],
                )
                if attempt >= retries or not self._is_retryable_error(exc):
                    if self._is_profile_in_use_error(exc):
                        raise RuntimeError("profile_in_use") from exc
                    if self._is_driver_connection_closed(exc):
                        raise RuntimeError("playwright_driver_connection_closed") from exc
                    if self._is_target_closed_error(exc):
                        raise RuntimeError("target_page_or_context_closed") from exc
                    raise
                await asyncio.sleep(min(1.8, 0.4 * attempt + 0.2))
        if last_exc:
            raise last_exc

    async def _page_evaluate(self, page: Any | None, script: str) -> Any:
        if not page:
            return None
        if self._sync_mode:
            return await self._run_sync(page.evaluate, script)
        return await page.evaluate(script)

    async def _scroll_snapshot(self) -> dict[str, float]:
        snapshot = await self._evaluate(
            """() => ({
                top: Number(window.scrollY || document.documentElement.scrollTop || 0),
                viewport: Number(window.innerHeight || document.documentElement.clientHeight || 0),
                total: Number(Math.max(
                    document.body ? document.body.scrollHeight : 0,
                    document.documentElement ? document.documentElement.scrollHeight : 0
                ) || 0)
            })"""
        )
        if not isinstance(snapshot, dict):
            return {"top": 0.0, "viewport": 0.0, "total": 0.0}
        return {
            "top": float(snapshot.get("top") or 0.0),
            "viewport": float(snapshot.get("viewport") or 0.0),
            "total": float(snapshot.get("total") or 0.0),
        }

    def _is_snapshot_at_bottom(self, snapshot: dict[str, float], margin_px: int = 180) -> bool:
        top = float(snapshot.get("top") or 0.0)
        viewport = float(snapshot.get("viewport") or 0.0)
        total = float(snapshot.get("total") or 0.0)
        if total <= 0:
            return False
        return (top + viewport) >= max(0.0, total - float(margin_px))

    async def _scroll_profile_surface_forward(self, kind: str) -> bool:
        before = await self._scroll_snapshot()
        step = random.randint(520, 1280) if kind == "reply" else random.randint(260, 760)
        await self._random_scroll(step)
        await self.human.jitter(180, 520 if kind == "reply" else 420)
        after = await self._scroll_snapshot()
        if self._is_snapshot_at_bottom(after):
            return True
        return abs(after.get("top", 0.0) - before.get("top", 0.0)) < 24.0

    def _is_retryable_error(self, exc: Exception) -> bool:
        return (
            self._is_target_closed_error(exc)
            or self._is_driver_connection_closed(exc)
            or "timeout" in str(exc).lower()
        )

    async def _refresh_page_handle(self) -> None:
        if not self.context:
            return
        if self.context.pages:
            self.page = self.context.pages[0]
            return
        if self._sync_mode:
            if not self._sync_context:
                return
            self.page = await self._run_sync(self._sync_context.new_page)
        else:
            self.page = await self.context.new_page()

    async def _goto(self, url: str) -> None:
        retries = max(1, int(self.NAV_RETRIES))
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if not self.page:
                    await self._refresh_page_handle()
                if not self.page:
                    raise RuntimeError("Browser context not started")
                if self._sync_mode:
                    await self._run_sync(self._sync_page.goto, url, wait_until="domcontentloaded")
                else:
                    await self.page.goto(url, wait_until="domcontentloaded")
                await self.human.jitter(300, 900)
                await self._wait_network_idle(1400)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "navigation_failed attempt=%s/%s url=%s error=%s",
                    attempt,
                    retries,
                    url,
                    str(exc)[:300],
                )
                if attempt >= retries or not self._is_retryable_error(exc):
                    if self._is_profile_in_use_error(exc):
                        raise RuntimeError("profile_in_use") from exc
                    if self._is_driver_connection_closed(exc):
                        raise RuntimeError("playwright_driver_connection_closed") from exc
                    if self._is_target_closed_error(exc):
                        raise RuntimeError("target_page_or_context_closed") from exc
                    raise
                await asyncio.sleep(min(1.8, 0.4 * attempt + 0.2))
                await self._refresh_page_handle()
        if last_exc:
            raise last_exc

    async def _go_back(self) -> None:
        if not self.page:
            return
        back_btn = await self._find_first(ui.BACK_BUTTONS, timeout_ms=900)
        if back_btn:
            with contextlib.suppress(Exception):
                await self._click(back_btn)
                await self.human.jitter(250, 700)
                await self._wait_network_idle(1100)
                return
        if self._sync_mode:
            await self._run_sync(self._sync_page.go_back, wait_until="domcontentloaded")
        else:
            await self.page.go_back(wait_until="domcontentloaded")
        await self.human.jitter(250, 700)
        await self._wait_network_idle(1100)

    async def _keyboard_press(self, shortcut: str) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._sync_page.keyboard.press, shortcut)
        else:
            await self.page.keyboard.press(shortcut)

    def _normalize_post_id(self, value: Any) -> str:
        match = self.POST_ID_RE.search(str(value or ""))
        return match.group(1) if match else ""

    def _normalize_username(self, username: str) -> str:
        return str(username or "").strip().lstrip("@")

    def _normalize_account_handle(self, username: str | None) -> str:
        handle = self._normalize_username(str(username or ""))
        if not handle or not self.ACCOUNT_HANDLE_RE.fullmatch(handle):
            return ""
        if handle.lower() in ui.RESERVED_PROFILE_PATHS:
            return ""
        return handle

    def _profile_handle_from_url(self, url: str) -> str:
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        return self._normalize_account_handle(parts[0])

    def _normalize_image_paths(self, image_paths: ImagePathInput | None) -> list[str]:
        if image_paths is None:
            return []
        if isinstance(image_paths, (str, os.PathLike)):
            candidates = [image_paths]
        else:
            candidates = list(image_paths)

        if len(candidates) > ui.MAX_IMAGES_PER_POST:
            raise ValueError(f"X image posts support up to {ui.MAX_IMAGES_PER_POST} images")

        normalized: list[str] = []
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            else:
                path = path.resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Image file does not exist: {path}")
            if path.suffix.lower() not in ui.SUPPORTED_IMAGE_EXTENSIONS:
                supported = ", ".join(sorted(ui.SUPPORTED_IMAGE_EXTENSIONS))
                raise ValueError(f"Unsupported image extension for X upload: {path.suffix or '<none>'}. Supported: {supported}")
            normalized.append(str(path))
        return normalized

    async def _is_compose_state(self) -> bool:
        if not self.page:
            return False
        url = self.page.url or ""
        return "/compose/" in url

    async def _wait_for_post_submission(self, timeout_seconds: float = 8.0) -> bool:
        if not self.page:
            return False
        deadline = time.monotonic() + max(1.5, timeout_seconds)
        while time.monotonic() < deadline:
            if not await self._is_compose_state():
                return True
            post_btn = await self._find_first(ui.POST_BUTTONS)
            if post_btn:
                disabled = (await self._get_attribute(post_btn, "aria-disabled") or "").strip().lower()
                if disabled == "true":
                    return True
            await asyncio.sleep(0.30)
        return False

    async def _submit_post(self) -> bool:
        if not self.page:
            return False
        for attempt in range(1, 6):
            post_btn = await self._find_first(ui.POST_BUTTONS, timeout_ms=900)
            if post_btn:
                disabled = (await self._get_attribute(post_btn, "aria-disabled") or "").strip().lower()
                if disabled not in {"true", "1"}:
                    try:
                        await self._click(post_btn)
                        await self.human.jitter(280, 760)
                        if await self._wait_for_post_submission(4.5):
                            return True
                    except Exception as exc:
                        logger.warning("submit_post_button_click_failed attempt=%s error=%s", attempt, str(exc)[:260])
            try:
                await self._keyboard_press("Control+Enter")
                await self.human.jitter(280, 760)
                if await self._wait_for_post_submission(4.5):
                    return True
            except Exception as exc:
                logger.warning("submit_post_shortcut_failed attempt=%s key=Control+Enter error=%s", attempt, str(exc)[:260])
            try:
                await self._keyboard_press("Meta+Enter")
                await self.human.jitter(220, 560)
                if await self._wait_for_post_submission(3.5):
                    return True
            except Exception:
                pass
        return False

    async def _is_visible(self, locator: Any) -> bool:
        try:
            if self._sync_mode:
                return bool(await self._run_sync(locator.is_visible))
            return bool(await locator.is_visible())
        except Exception:
            return False

    async def _has_reply_audience_modal(self) -> bool:
        if not self.page:
            return False
        for modal_selector in ui.REPLY_AUDIENCE_MODAL_SELECTORS:
            modal = self.page.locator(modal_selector).first
            if not await self._count_locator(modal):
                continue
            if await self._is_visible(modal):
                return True
        return False

    async def _find_reply_audience_done_button(self, modal: Any) -> Any | None:
        if not self.page or modal is None:
            return None
        candidates: list[Any] = []
        with contextlib.suppress(Exception):
            candidates.append(modal.get_by_role("button", name=re.compile(r"^\s*Done\s*$", re.IGNORECASE)).first)
        for selector in ui.REPLY_AUDIENCE_DONE_BUTTONS:
            with contextlib.suppress(Exception):
                candidates.append(modal.locator(selector).first)
        with contextlib.suppress(Exception):
            text_span = modal.locator('span:has-text("Done")').first
            if await self._count_locator(text_span):
                candidates.append(text_span.locator("xpath=ancestor::*[self::button or @role='button'][1]").first)

        for candidate in candidates:
            try:
                if not await self._count_locator(candidate):
                    continue
                if not await self._is_visible(candidate):
                    continue
                if not await self._is_button_enabled(candidate):
                    continue
                return candidate
            except Exception:
                continue
        return None

    async def _click_reply_audience_done_dom(self) -> bool:
        if not self.page:
            return False
        script = """
(() => {
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  const modal = dialogs.find(d => {
    const close = d.querySelector('[data-testid="app-bar-close"]');
    if (!close) return false;
    const txt = (d.innerText || d.textContent || '').toLowerCase();
    return txt.includes('replying to') && txt.includes('done');
  });
  if (!modal) return false;
  const candidates = Array.from(modal.querySelectorAll('button,[role="button"]'));
  for (const node of candidates) {
    const txt = (node.innerText || node.textContent || '').trim().toLowerCase();
    if (txt !== 'done') continue;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    if (!rect || rect.width < 3 || rect.height < 3) continue;
    if (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') continue;
    node.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    node.click();
    return true;
  }
  return false;
})()
"""
        try:
            if self._sync_mode:
                return bool(await self._run_sync(self._sync_page.evaluate, script))
            return bool(await self.page.evaluate(script))
        except Exception:
            return False

    async def _confirm_reply_audience_if_needed(self) -> bool:
        if not self.page:
            return False
        if not await self._has_reply_audience_modal():
            return False
        logger.info("reply_audience_modal_detected")
        active_modal = None
        for modal_selector in ui.REPLY_AUDIENCE_MODAL_SELECTORS:
            modal = self.page.locator(modal_selector).first
            if not await self._count_locator(modal):
                continue
            if not await self._is_visible(modal):
                continue
            active_modal = modal
            break
        if active_modal is None:
            logger.warning("reply_audience_modal_detected_but_scope_missing")
            return False

        for attempt in range(1, 5):
            done_btn = await self._find_reply_audience_done_button(active_modal)
            if done_btn:
                clicked = False
                try:
                    await self._click(done_btn)
                    clicked = True
                except Exception:
                    with contextlib.suppress(Exception):
                        await self._click_force(done_btn)
                        clicked = True
                if clicked:
                    logger.info("reply_audience_done_click attempt=%s", attempt)
            else:
                logger.warning("reply_audience_done_button_not_found attempt=%s", attempt)

            if await self._has_reply_audience_modal():
                dom_clicked = await self._click_reply_audience_done_dom()
                if dom_clicked:
                    logger.info("reply_audience_done_dom_click attempt=%s", attempt)

            await self.human.jitter(220, 680)
            await self._wait_network_idle(800)
            for _ in range(10):
                if not await self._has_reply_audience_modal():
                    logger.info("reply_audience_modal_closed")
                    return True
                await asyncio.sleep(0.10)
            logger.info("reply_audience_done_retry attempt=%s", attempt)

        logger.warning("reply_audience_modal_still_open_after_done")
        return False

    async def _return_home(self, force_nav: bool = False) -> bool:
        if not self.page:
            return False
        if await self._looks_like_home_timeline():
            return True
        before = self.page.url or ""
        for _ in range(3):
            try:
                await self._go_back()
            except Exception:
                break
            if await self._looks_like_home_timeline():
                return True
            current = self.page.url or ""
            if current == before:
                break
            before = current
        if await self._open_home_via_click():
            return True
        if force_nav:
            await self._goto(f"{self.BASE_URL}/home")
            return await self._looks_like_home_timeline()
        return False

    async def _reload_current_page(self) -> None:
        if not self.page:
            return
        if self._sync_mode:
            await self._run_sync(self._sync_page.reload, wait_until="domcontentloaded")
        else:
            await self.page.reload(wait_until="domcontentloaded")
        await self.human.jitter(260, 780)
        await self._wait_network_idle(1300)

    async def return_home(self, force_refresh: bool = False) -> bool:
        if not self.page:
            return False
        home_ready = await self._return_home(force_nav=force_refresh)
        if not home_ready:
            return False
        if not force_refresh:
            return True
        try:
            await self._reload_current_page()
        except Exception as exc:
            logger.warning("return_home_refresh_failed error=%s", str(exc)[:260])
            return False
        return await self._looks_like_home_timeline()

    async def settle_after_action(
        self,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> bool:
        requested_tab = str(tab or "for_you").strip().lower().replace("-", "_").replace(" ", "_")
        if requested_tab not in {"for_you", "following"}:
            requested_tab = "for_you"
        if force_refresh:
            settled = await self.return_home(force_refresh=True)
            if settled:
                settled = await self._select_home_tab(requested_tab)
        else:
            settled = await self.settle_home(requested_tab)
        if settled and reset_scroll and self.page:
            await self._keyboard_press("Home")
            await self.human.jitter(180, 520)
        return settled

    async def _find_first(self, selectors: list[str], timeout_ms: int = 0):
        if not self.page:
            raise RuntimeError("Browser context not started")
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                if await self._count_locator(locator):
                    return locator.first
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _is_button_enabled(self, locator: Any) -> bool:
        aria_disabled = (await self._get_attribute(locator, "aria-disabled") or "").strip().lower()
        disabled_attr = (await self._get_attribute(locator, "disabled") or "").strip().lower()
        return aria_disabled not in {"true", "1"} and disabled_attr not in {"true", "1", "disabled"}

    async def _find_first_enabled(self, selectors: list[str], timeout_ms: int = 0):
        if not self.page:
            raise RuntimeError("Browser context not started")
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = self.page.locator(selector)
                count = await self._count_locator(locator)
                for i in range(count):
                    candidate = locator.nth(i)
                    if await self._is_button_enabled(candidate):
                        return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.16)

    async def _find_first_in_scope(
        self,
        scope: Any,
        selectors: list[str],
        timeout_ms: int = 0,
        require_enabled: bool = False,
    ):
        deadline = time.monotonic() + (max(0, timeout_ms) / 1000.0)
        while True:
            for selector in selectors:
                locator = scope.locator(selector)
                count = await self._count_locator(locator)
                for idx in range(count):
                    candidate = locator.nth(idx)
                    if require_enabled and not await self._is_button_enabled(candidate):
                        continue
                    return candidate
            if timeout_ms <= 0 or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.14)

    async def _find_reply_submit_button(self, textbox: Any | None = None, timeout_ms: int = 1400):
        if not self.page:
            return None
        scopes: list[Any] = []
        if textbox is not None:
            with contextlib.suppress(Exception):
                dialog = textbox.locator("xpath=ancestor::*[@role='dialog'][1]").first
                if await self._count_locator(dialog) and await self._is_visible(dialog):
                    scopes.append(dialog)
            with contextlib.suppress(Exception):
                parent = textbox.locator("xpath=ancestor::*[self::form or self::article][1]").first
                if await self._count_locator(parent) and await self._is_visible(parent):
                    scopes.append(parent)
        scopes.append(self.page)

        for scope in scopes:
            btn = await self._find_first_in_scope(
                scope,
                ui.REPLY_SEND_BUTTONS,
                timeout_ms=timeout_ms,
                require_enabled=True,
            )
            if not btn:
                continue
            with contextlib.suppress(Exception):
                testid = (await self._get_attribute(btn, "data-testid")) or ""
                label = (await self._inner_text(btn) or "").strip().replace("\n", " ")[:80]
                logger.info("reply_submit_candidate testid=%s label=%s", testid or "-", label or "-")
            return btn

        return await self._find_first_enabled(ui.REPLY_SEND_BUTTONS, timeout_ms=timeout_ms)

    async def _like_in_current_context(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        current_url = self.page.url or ""

        # Status page path: click the visible post like control without extra navigation.
        if f"/status/{post_id}" in current_url:
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if not await self._count_locator(article):
                article = self.page.locator("article").first
            active_btn = await self._find_first_in_scope(article, ui.LIKE_ACTIVE_BUTTONS, timeout_ms=700)
            if active_btn:
                return True
            like_btn = await self._find_first_in_scope(article, ui.LIKE_BUTTONS, timeout_ms=1200, require_enabled=True)
            if not like_btn:
                return False
            await self._click(like_btn)
            await self.human.jitter(350, 900)
            return True

        # Feed/search/profile path: locate target article and click inline like.
        scan_rounds = 1
        for round_idx in range(scan_rounds):
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if await self._count_locator(article):
                active_btn = await self._find_first_in_scope(article, ui.LIKE_ACTIVE_BUTTONS, timeout_ms=450)
                if active_btn:
                    return True
                like_btn = await self._find_first_in_scope(
                    article,
                    ui.LIKE_BUTTONS,
                    timeout_ms=900,
                    require_enabled=True,
                )
                if like_btn:
                    await self._click(like_btn)
                    await self.human.jitter(350, 900)
                    return True
        return False

    async def _find_post_article_in_context(self, platform_post_id: str, scan_rounds: int = 1) -> Any | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None

        current_url = self.page.url or ""
        if f"/status/{post_id}" in current_url:
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if not await self._count_locator(article):
                article = self.page.locator("article").first
            if await self._count_locator(article):
                return article

        rounds = max(1, int(scan_rounds))
        for idx in range(rounds):
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if await self._count_locator(article):
                return article
            if idx < rounds - 1:
                await self._random_scroll(random.randint(240, 760))
                await self.human.jitter(130, 380)
        return None

    async def _dismiss_reply_ui(self) -> None:
        if not self.page:
            return
        close_btn = await self._find_first(ui.REPLY_CLOSE_BUTTONS, timeout_ms=400)
        if close_btn:
            with contextlib.suppress(Exception):
                await self._click(close_btn)
                await self.human.jitter(160, 460)
                await self._wait_network_idle(700)
                return
        with contextlib.suppress(Exception):
            await self._keyboard_press("Escape")
            await self.human.jitter(140, 380)

    async def _open_reply_box_in_current_context(self, platform_post_id: str, scan_rounds: int = 2) -> Any | None:
        if not self.page:
            return None
        article = await self._find_post_article_in_context(platform_post_id, scan_rounds=max(1, scan_rounds))
        reply_btn = None
        if article:
            reply_btn = await self._find_first_in_scope(
                article,
                ui.COMMENT_BUTTONS,
                timeout_ms=1100,
                require_enabled=True,
            )
        if not reply_btn:
            post_id = self._normalize_post_id(platform_post_id)
            if post_id and f"/status/{post_id}" in (self.page.url or ""):
                reply_btn = await self._find_first_enabled(ui.COMMENT_BUTTONS, timeout_ms=900)
        if not reply_btn:
            return None

        await self._click(reply_btn)
        await self.human.jitter(280, 900)
        box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=2600)
        if box:
            return box
        if await self._confirm_reply_audience_if_needed():
            box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=1400)
            if box:
                return box
        return None

    async def _open_quote_box_in_current_context(self, platform_post_id: str, scan_rounds: int = 2) -> Any | None:
        if not self.page:
            return None
        article = await self._find_post_article_in_context(platform_post_id, scan_rounds=max(1, scan_rounds))
        repost_btn = None
        if article:
            repost_btn = await self._find_first_in_scope(
                article,
                ui.REPOST_BUTTONS,
                timeout_ms=1400,
                require_enabled=True,
            )
        post_id = self._normalize_post_id(platform_post_id)
        if not repost_btn and post_id and f"/status/{post_id}" in (self.page.url or ""):
            repost_btn = await self._find_first_enabled(ui.REPOST_BUTTONS, timeout_ms=1200)
        if not repost_btn:
            return None

        await self._click(repost_btn)
        await self.human.jitter(220, 700)
        quote_item = await self._find_first_enabled(ui.QUOTE_MENU_ITEMS, timeout_ms=2600)
        if not quote_item:
            quote_item = await self._find_first(ui.QUOTE_MENU_ITEMS, timeout_ms=800)
        if not quote_item:
            logger.warning("quote_menu_item_not_found target=%s", post_id or platform_post_id)
            with contextlib.suppress(Exception):
                await self._keyboard_press("Escape")
                await self.human.jitter(120, 360)
            return None
        await self._click(quote_item)
        await self.human.jitter(350, 1000)
        box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=3200)
        if box:
            return box
        with contextlib.suppress(Exception):
            await self._keyboard_press("Escape")
            await self.human.jitter(120, 360)
        return None

    async def _looks_like_home_timeline(self) -> bool:
        if not self.page:
            return False
        if await self._any_selector(ui.HOME_SELECTORS):
            return True
        if "/home" in (self.page.url or ""):
            return True
        primary = self.page.locator('div[data-testid="primaryColumn"]').first
        if not await self._count_locator(primary):
            return False
        aria_label = (await self._get_attribute(primary, "aria-label") or "").lower()
        aria_labelledby = (await self._get_attribute(primary, "aria-labelledby") or "").lower()
        if "home timeline" in aria_label:
            return True
        return "home" in aria_labelledby and "timeline" in aria_labelledby

    async def _looks_like_notifications_page(self) -> bool:
        if not self.page:
            return False
        url = (self.page.url or "").lower()
        if "/notifications" in url:
            return True
        return await self._any_selector(ui.NOTIFICATIONS_ACTIVE_SELECTORS)

    async def _open_home_via_click(self) -> bool:
        if not self.page:
            return False
        if await self._looks_like_home_timeline():
            return True
        for _ in range(2):
            home_btn = await self._find_first(ui.HOME_ENTRY_SELECTORS, timeout_ms=1200)
            if home_btn:
                try:
                    await self._click(home_btn)
                    await self.human.jitter(220, 620)
                    await self._wait_network_idle(1200)
                except Exception:
                    pass
                if await self._looks_like_home_timeline():
                    return True
        return False

    async def _select_home_tab(self, tab: str) -> bool:
        target = str(tab or "for_you").strip().lower()
        if target in {"for-you", "for you", "foryou"}:
            target = "for_you"
        if target not in {"for_you", "following"}:
            return False
        selectors = ui.HOME_FOLLOWING_TAB_SELECTORS if target == "following" else ui.HOME_FOR_YOU_TAB_SELECTORS
        active = await self._active_home_tab()
        if active == target:
            return True
        tab_node = await self._find_first(selectors, timeout_ms=1600)
        if not tab_node:
            return False
        with contextlib.suppress(Exception):
            await self._click(tab_node)
            await self.human.jitter(260, 760)
            await self._wait_network_idle(1200)
        active = await self._active_home_tab()
        return active in {"", target}

    async def settle_home(self, tab: str = "for_you", force_nav: bool = False) -> bool:
        if not self.page:
            return False
        if force_nav or not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                await self._goto(f"{self.BASE_URL}/home")
        if not await self._looks_like_home_timeline():
            return False
        return await self._select_home_tab(tab)

    async def _open_notifications_via_click(self) -> bool:
        if not self.page:
            return False
        if await self._looks_like_notifications_page():
            return True
        for _ in range(2):
            bell = await self._find_first(ui.NOTIFICATIONS_ENTRY_SELECTORS, timeout_ms=1300)
            if not bell:
                continue
            with contextlib.suppress(Exception):
                await self._click(bell)
                await self.human.jitter(220, 620)
                await self._wait_network_idle(1400)
            if await self._looks_like_notifications_page():
                return True
        return False

    async def _open_notifications_mentions_tab(self) -> bool:
        if not self.page:
            return False
        for _ in range(3):
            tab = await self._find_first(ui.NOTIFICATIONS_MENTIONS_TAB_SELECTORS, timeout_ms=1200)
            if not tab:
                break
            with contextlib.suppress(Exception):
                await self._click(tab)
                await self.human.jitter(220, 620)
                await self._wait_network_idle(1200)
            current_url = (self.page.url or "").lower()
            if "/notifications/mentions" in current_url:
                return True
            with contextlib.suppress(Exception):
                aria_current = (await self._get_attribute(tab, "aria-current") or "").strip().lower()
                if aria_current == "page":
                    return True
        return "/notifications/mentions" in ((self.page.url or "").lower())

    async def _clear_input_like_human(self) -> None:
        try:
            await self._keyboard_press("Control+A")
            await self._keyboard_press("Backspace")
            await self.human.jitter(60, 220)
        except Exception:
            pass

    async def _open_search_query(self, query: str) -> bool:
        if not self.page:
            return False
        self._clear_action_error()
        query_text = (query or "").strip()
        if not query_text:
            return False

        # Prefer UI navigation first.
        if not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/home")

        entry = await self._find_first(ui.SEARCH_ENTRY_SELECTORS, timeout_ms=1200)
        if entry:
            with contextlib.suppress(Exception):
                await self._click(entry)
                await self.human.jitter(180, 480)

        search_box = await self._find_first(ui.SEARCH_INPUT_SELECTORS, timeout_ms=2500)
        if not search_box:
            return False
        try:
            await self._click(search_box)
            await self._clear_input_like_human()
            await self._type_text(search_box, query_text)
            await self.human.jitter(120, 360)
            await self._keyboard_press("Enter")
            await self.human.jitter(300, 900)
            await self._wait_network_idle(1400)
            return "/search" in ((self.page.url or "").lower())
        except Exception as exc:
            self._handle_soft_ui_error(
                "open_search_query",
                exc,
                selector=" / ".join(ui.SEARCH_INPUT_SELECTORS),
            )
            return False

    async def _set_search_mode(self, mode: str) -> bool:
        if not self.page:
            return False
        self._clear_action_error()
        target_mode = (mode or "").strip().lower()
        if target_mode not in {"live", "top"}:
            return False
        selectors = ui.SEARCH_TAB_LATEST_SELECTORS if target_mode == "live" else ui.SEARCH_TAB_TOP_SELECTORS
        tab = await self._find_first(selectors, timeout_ms=1400)
        if not tab:
            return False
        try:
            await self._click(tab)
            await self.human.jitter(220, 620)
            await self._wait_network_idle(1200)
            return True
        except Exception as exc:
            self._handle_soft_ui_error("set_search_mode", exc, selector=" / ".join(selectors))
            return False

    async def _open_profile_page(self, username: str) -> bool:
        if not self.page:
            return False
        handle = self._normalize_username(username)
        if not handle:
            return False
        # Try click-based discovery from current feed/search first.
        direct = self.page.locator(f'a[href^="/{handle}"]').first
        if await self._count_locator(direct):
            with contextlib.suppress(Exception):
                await self._click(direct)
                await self.human.jitter(260, 780)
                await self._wait_network_idle(1200)
                return True

        # Fallback: open via search UI and click profile result.
        searched = await self._open_search_query(f"@{handle}")
        if searched:
            profile_link = self.page.locator(f'a[href^="/{handle}"]').first
            if await self._count_locator(profile_link):
                with contextlib.suppress(Exception):
                    await self._click(profile_link)
                    await self.human.jitter(280, 860)
                    await self._wait_network_idle(1400)
                    return True
        return False

    def _extract_profile_handle_from_href(self, href: str) -> str:
        raw = str(href or "").strip()
        if not raw:
            return ""
        if raw.startswith("http"):
            match = self.PROFILE_URL_RE.search(raw)
            if match:
                return self._normalize_username(match.group(1))
        path = raw.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not path or "/" in path:
            return ""
        if path.lower() in ui.RESERVED_PROFILE_PATHS:
            return ""
        return self._normalize_username(path)

    def _extract_handle_from_text(self, text: str) -> str:
        match = self.HANDLE_TEXT_RE.search(str(text or ""))
        return self._normalize_username(match.group(1) if match else "")

    async def _get_authenticated_handle(self, force_refresh: bool = False) -> str | None:
        if self._authenticated_handle and not force_refresh:
            return self._authenticated_handle
        if not self.page:
            return None
        for attempt in range(2):
            for selector in ui.PROFILE_ENTRY_SELECTORS:
                locator = self.page.locator(selector).first
                if not await self._count_locator(locator):
                    continue
                href = await self._get_attribute(locator, "href")
                handle = self._extract_profile_handle_from_href(href or "")
                if not handle:
                    text = ""
                    with contextlib.suppress(Exception):
                        text = await self._inner_text(locator)
                    handle = self._extract_handle_from_text(text)
                if handle:
                    self._authenticated_handle = handle
                    return handle

            account_switcher = self.page.locator('[data-testid="SideNav_AccountSwitcher_Button"]').first
            if await self._count_locator(account_switcher):
                with contextlib.suppress(Exception):
                    text = await self._inner_text(account_switcher)
                    handle = self._extract_handle_from_text(text)
                    if handle:
                        self._authenticated_handle = handle
                        return handle

            if attempt == 0 and not await self._looks_like_home_timeline():
                with contextlib.suppress(Exception):
                    if not await self._open_home_via_click():
                        await self._goto(f"{self.BASE_URL}/home")
        return None

    async def _extract_article_author_handle(self, article: Any) -> str:
        if not article:
            return ""
        auth_locator = article.locator('div[data-testid="User-Name"] a[href^="/"]').first
        if await self._count_locator(auth_locator):
            href = (await self._get_attribute(auth_locator, "href")) or ""
            handle = self._extract_profile_handle_from_href(href)
            if handle:
                return handle
        with contextlib.suppress(Exception):
            name_block = article.locator('div[data-testid="User-Name"]').first
            if await self._count_locator(name_block):
                return self._extract_handle_from_text(await self._inner_text(name_block))
        return ""

    async def _extract_article_author_display_name(self, article: Any) -> str:
        if not article:
            return ""
        with contextlib.suppress(Exception):
            name_block = article.locator('div[data-testid="User-Name"]').first
            if await self._count_locator(name_block):
                for line in (await self._inner_text(name_block, timeout_ms=1200)).splitlines():
                    clean = line.strip()
                    if clean and not clean.startswith("@"):
                        return clean
        return ""

    async def _extract_article_media(self, article: Any) -> list[dict[str, Any]]:
        if not article:
            return []
        media: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            images = article.locator("img")
            for idx in range(min(await self._count_locator(images), 12)):
                image = images.nth(idx)
                src = (await self._get_attribute(image, "src")) or ""
                alt = (await self._get_attribute(image, "alt")) or ""
                if not src or "profile_images" in src:
                    continue
                media.append(ObservedMediaData(kind="image", url=src, thumbnail_url=src, alt_text=alt).to_dict())
        with contextlib.suppress(Exception):
            videos = article.locator("video")
            for idx in range(min(await self._count_locator(videos), 6)):
                video = videos.nth(idx)
                src = (await self._get_attribute(video, "src")) or ""
                poster = (await self._get_attribute(video, "poster")) or ""
                media.append(ObservedMediaData(kind="video", url=src, thumbnail_url=poster).to_dict())
        return media

    async def _screenshot_locator(self, locator: Any, path: Path) -> bool:
        try:
            if self._sync_mode:
                await self._run_sync(locator.screenshot, path=str(path))
            else:
                await locator.screenshot(path=str(path))
            return True
        except Exception as exc:
            logger.warning("media_capture_screenshot_failed path=%s error=%s", path, str(exc)[:220])
            return False

    async def _play_video_locator(self, locator: Any) -> None:
        script = """node => {
            try {
                node.muted = true;
                const result = node.play && node.play();
                if (result && result.catch) result.catch(() => {});
            } catch (_err) {}
        }"""
        with contextlib.suppress(Exception):
            if self._sync_mode:
                await self._run_sync(locator.evaluate, script)
            else:
                await locator.evaluate(script)

    async def _capture_article_media_nodes(
        self,
        article: Any,
        target_post_id: str,
        output_dir: Path,
        frame_count: int = 3,
    ) -> list[MediaCaptureData]:
        if not article:
            return []
        captures: list[MediaCaptureData] = []
        seen_sources: set[str] = set()

        with contextlib.suppress(Exception):
            images = article.locator("img")
            for idx in range(min(await self._count_locator(images), 24)):
                image = images.nth(idx)
                src = (await self._get_attribute(image, "src")) or ""
                alt = (await self._get_attribute(image, "alt")) or ""
                if not src or "profile_images" in src or src in seen_sources:
                    continue
                seen_sources.add(src)
                path = output_dir / f"{target_post_id}_image_{idx + 1}.png"
                ok = await self._screenshot_locator(image, path)
                captures.append(
                    MediaCaptureData(
                        kind="image",
                        path=str(path) if ok else "",
                        target_post_id=target_post_id,
                        source_url=src,
                        thumbnail_url=src,
                        alt_text=alt,
                        raw={"selector": "img", "index": idx, "screenshot_ok": ok},
                    )
                )

        max_frames = max(1, min(int(frame_count), 10))
        with contextlib.suppress(Exception):
            videos = article.locator("video")
            for video_idx in range(min(await self._count_locator(videos), 6)):
                video = videos.nth(video_idx)
                src = (await self._get_attribute(video, "src")) or ""
                poster = (await self._get_attribute(video, "poster")) or ""
                await self._play_video_locator(video)
                for frame_idx in range(max_frames):
                    path = output_dir / f"{target_post_id}_video_{video_idx + 1}_frame_{frame_idx + 1}.png"
                    ok = await self._screenshot_locator(video, path)
                    captures.append(
                        MediaCaptureData(
                            kind="video",
                            path=str(path) if ok else "",
                            target_post_id=target_post_id,
                            source_url=src,
                            thumbnail_url=poster,
                            raw={
                                "selector": "video",
                                "index": video_idx,
                                "frame_index": frame_idx,
                                "screenshot_ok": ok,
                            },
                        )
                    )
                    if frame_idx < max_frames - 1:
                        await asyncio.sleep(0.35)
        return captures

    async def _extract_article_status_url(self, article: Any) -> str:
        urls = await self._extract_article_status_urls(article)
        return urls[0] if urls else ""

    async def _extract_article_status_urls(self, article: Any) -> list[str]:
        if not article:
            return []
        links = article.locator('a[href*="/status/"]')
        total = await self._count_locator(links)
        if not total:
            return []
        urls: list[str] = []
        for idx in range(min(total, 20)):
            href = (await self._get_attribute(links.nth(idx), "href")) or ""
            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = f"{self.BASE_URL}{href}"
            else:
                url = href
            if url and url not in urls:
                urls.append(url)
        return urls

    def _extract_status_ids_from_urls(self, urls: Sequence[str]) -> list[str]:
        post_ids: list[str] = []
        for url in urls:
            post_id = self._extract_post_id(str(url or ""))
            if post_id and post_id not in post_ids:
                post_ids.append(post_id)
        return post_ids

    async def _extract_article_social_context(self, article: Any) -> str:
        if not article:
            return ""
        context_node = article.locator('[data-testid="socialContext"]').first
        if not await self._count_locator(context_node):
            return ""
        with contextlib.suppress(Exception):
            return (await self._inner_text(context_node, timeout_ms=1200)).strip()
        return ""

    def _normalize_article_notice(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _extract_matching_notice(self, text: str, patterns: Sequence[re.Pattern[str]]) -> str:
        compact = self._normalize_article_notice(text)
        if not compact:
            return ""
        for pattern in patterns:
            match = pattern.search(compact)
            if match:
                return match.group(0).strip()
        return ""

    def _extract_article_author_limit_state(self, body: str, social_context: str = "") -> dict[str, Any]:
        raw = "\n".join(part for part in (body, social_context) if part)
        reply_notice = self._extract_matching_notice(raw, ui.REPLY_LIMIT_NOTICE_PATTERNS)
        reply_blocked_notice = self._extract_matching_notice(raw, ui.REPLY_LIMIT_BLOCKED_PATTERNS)
        author_limited = bool(reply_notice or reply_blocked_notice)
        notice = reply_notice or reply_blocked_notice
        limit_type = "reply" if author_limited else ""
        return {
            "author_limited": author_limited,
            "author_limit_type": limit_type,
            "author_limit_notice": notice,
            "reply_limited": author_limited,
            "reply_limit_notice": notice,
            "reply_limit_blocked": bool(reply_blocked_notice),
        }

    async def _article_has_reply_context(self, article: Any, body: str = "") -> bool:
        if not article:
            return False
        if "replying to" in str(body or "").lower():
            return True
        reply_markers = [
            'span:has-text("Replying to")',
            'div:has-text("Replying to")',
            'a:has-text("Replying to")',
        ]
        for selector in reply_markers:
            with contextlib.suppress(Exception):
                marker = article.locator(selector).first
                if await self._count_locator(marker):
                    return True
        return False

    async def _article_has_quote_context(self, article: Any, body: str = "") -> bool:
        if not article:
            return False
        if await self._article_has_reply_context(article, body=body):
            return False
        urls = await self._extract_article_status_urls(article)
        return len(self._extract_status_ids_from_urls(urls)) >= 2

    async def _classify_profile_article(self, article: Any, own_handle: str) -> dict[str, Any]:
        url = await self._extract_article_status_url(article)
        post_id = self._extract_post_id(url)
        author = await self._extract_article_author_handle(article)
        text = (await self._extract_article_text(article))[:4000]
        body = ""
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()
        social_context = await self._extract_article_social_context(article)
        own_handle_lower = own_handle.lower()
        author_lower = author.lower()
        social_lower = social_context.lower()
        body_lower = body.lower()
        is_repost = bool("reposted" in social_lower and author_lower != own_handle_lower)
        reply_context = await self._article_has_reply_context(article, body=body)
        quote_context = await self._article_has_quote_context(article, body=body)
        is_reply = bool(author_lower == own_handle_lower and ("replying to" in body_lower or reply_context))
        is_quote = bool(author_lower == own_handle_lower and not is_repost and not is_reply and quote_context)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        return {
            "post_id": post_id,
            "url": url or (f"{self.BASE_URL}/i/web/status/{post_id}" if post_id else ""),
            "author": author,
            "text": text,
            "social_context": social_context,
            "is_reply": is_reply,
            "is_repost": is_repost,
            "is_quote": is_quote,
            **limit_state,
        }

    def _profile_item_matches_kind(self, item: dict[str, Any], kind: str, own_handle: str) -> bool:
        author = str(item.get("author") or "").lower()
        own_handle_lower = own_handle.lower()
        if kind == "repost":
            return bool(item.get("is_repost"))
        if author != own_handle_lower:
            return False
        if kind == "reply":
            return bool(item.get("is_reply"))
        if kind == "quote":
            return bool(item.get("is_quote"))
        if kind == "post":
            return (
                not bool(item.get("is_reply"))
                and not bool(item.get("is_repost"))
                and not bool(item.get("is_quote"))
            )
        return False

    def _profile_surface_url(self, own_handle: str, kind: str) -> str:
        handle = self._normalize_username(own_handle)
        if kind == "reply":
            return f"{self.BASE_URL}/{handle}/with_replies"
        return f"{self.BASE_URL}/{handle}"

    async def _open_profile_surface(self, own_handle: str, kind: str) -> bool:
        if not self.page:
            return False
        target_url = self._profile_surface_url(own_handle, kind)
        current = (self.page.url or "").rstrip("/")
        if current == target_url.rstrip("/"):
            return True
        try:
            await self._goto(target_url)
            await self.human.jitter(280, 760)
            return True
        except Exception as exc:
            logger.warning(
                "open_profile_surface_failed kind=%s handle=%s error=%s",
                kind,
                own_handle,
                str(exc)[:260],
            )
            return False

    async def _find_profile_article_by_post_id(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
        scan_rounds: int = 7,
    ) -> Any | None:
        if not self.page:
            return None
        if not await self._open_profile_surface(own_handle, kind):
            return None
        rounds = max(2, int(scan_rounds))
        for round_idx in range(rounds):
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if await self._count_locator(article):
                return article
            articles = self.page.locator("article")
            total = await self._count_locator(articles)
            for idx in range(min(total, 80)):
                candidate = articles.nth(idx)
                href = await self._extract_article_status_url(candidate)
                if self._extract_post_id(href) == post_id:
                    return candidate
            if round_idx < rounds - 1:
                await self._random_scroll(random.randint(380, 1160))
                await self.human.jitter(220, 620)
        return None

    async def _find_owned_status_article_by_post_id(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        if not self.page:
            return None
        try:
            await self._goto(f"{self.BASE_URL}/i/web/status/{post_id}")
        except Exception as exc:
            logger.warning("open_direct_delete_status_failed target=%s error=%s", post_id, str(exc)[:260])
            return None
        article = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if article is None:
            return None
        item = await self._classify_profile_article(article, own_handle)
        if str(item.get("post_id") or "") != post_id:
            urls = await self._extract_article_status_urls(article)
            if post_id not in self._extract_status_ids_from_urls(urls):
                return None
            item["post_id"] = post_id
            item["url"] = f"{self.BASE_URL}/i/web/status/{post_id}"
        if not self._profile_item_matches_kind(item, kind, own_handle):
            logger.warning(
                "direct_delete_status_kind_mismatch target=%s kind=%s author=%s is_reply=%s is_repost=%s is_quote=%s",
                post_id,
                kind,
                item.get("author") or "",
                bool(item.get("is_reply")),
                bool(item.get("is_repost")),
                bool(item.get("is_quote")),
            )
            return None
        return article, item

    async def _wait_for_profile_article_disappearance(
        self,
        own_handle: str,
        post_id: str,
        kind: str,
        retries: int = 14,
    ) -> bool:
        if not self.page:
            return True
        for _ in range(max(4, retries)):
            article = self.page.locator(f'article:has(a[href*="/status/{post_id}"])').first
            if not await self._count_locator(article):
                return True
            await asyncio.sleep(0.25)
        return False

    async def _collect_visible_profile_candidates(
        self,
        own_handle: str,
        kind: str,
        limit: int = 8,
        excluded_post_ids: set[str] | None = None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        if not self.page:
            return []
        excluded = excluded_post_ids or set()
        articles = self.page.locator("article")
        total = await self._count_locator(articles)
        rows: list[tuple[Any, dict[str, Any]]] = []
        scan_cap = max(limit * 8, 60 if kind == "reply" else 32)
        for idx in range(min(total, scan_cap)):
            article = articles.nth(idx)
            item = await self._classify_profile_article(article, own_handle)
            post_id = str(item.get("post_id") or "")
            if not post_id or post_id in excluded:
                continue
            if not self._profile_item_matches_kind(item, kind, own_handle):
                continue
            rows.append((article, item))
            if len(rows) >= limit:
                return rows
        return rows

    async def _collect_profile_items(self, own_handle: str, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.page:
            return []
        if not await self._open_profile_surface(own_handle, kind):
            return []

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stagnation_rounds = 0
        scroll_rounds = max(4, (max(1, int(limit)) // 4) + 6)
        for round_idx in range(scroll_rounds):
            articles = self.page.locator("article")
            total = await self._count_locator(articles)
            new_items = 0
            for idx in range(min(total, max(limit * 3, 70))):
                article = articles.nth(idx)
                item = await self._classify_profile_article(article, own_handle)
                post_id = str(item.get("post_id") or "")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                if not self._profile_item_matches_kind(item, kind, own_handle):
                    continue
                rows.append(item)
                new_items += 1
                if len(rows) >= limit:
                    return rows
            if round_idx < scroll_rounds - 1:
                if new_items == 0:
                    stagnation_rounds += 1
                else:
                    stagnation_rounds = 0
                if stagnation_rounds >= 3:
                    break
                await self._random_scroll(random.randint(380, 1180))
                await self.human.jitter(260, 680)
        return rows

    async def _collect_recent_owned_created_candidates(
        self,
        own_handle: str,
        kind: str,
        limit: int = 36,
    ) -> list[dict[str, Any]]:
        if not self.page:
            return []
        own_handle_lower = own_handle.lower()
        if kind == "reply":
            surfaces = ("reply", "post")
        elif kind == "quote":
            surfaces = ("post", "reply")
        else:
            surfaces = ("post", "reply")

        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        per_surface_limit = max(12, min(max(1, int(limit)), 80))
        for surface_kind in surfaces:
            if len(rows) >= limit:
                break
            if not await self._open_profile_surface(own_handle, surface_kind):
                continue
            stagnation_rounds = 0
            scroll_rounds = max(3, (per_surface_limit // 5) + 4)
            for round_idx in range(scroll_rounds):
                articles = self.page.locator("article")
                total = await self._count_locator(articles)
                new_items = 0
                for idx in range(min(total, max(per_surface_limit * 3, 70))):
                    article = articles.nth(idx)
                    item = await self._classify_profile_article(article, own_handle)
                    post_id = str(item.get("post_id") or "")
                    author = str(item.get("author") or "").lower()
                    if not post_id or post_id in seen_ids or author != own_handle_lower:
                        continue
                    seen_ids.add(post_id)
                    if bool(item.get("is_repost")):
                        continue
                    item["source_surface"] = "with_replies" if surface_kind == "reply" else "posts"
                    rows.append(item)
                    new_items += 1
                    if len(rows) >= limit:
                        return rows
                if round_idx < scroll_rounds - 1:
                    if new_items == 0:
                        stagnation_rounds += 1
                    else:
                        stagnation_rounds = 0
                    if stagnation_rounds >= 2:
                        break
                    await self._random_scroll(random.randint(380, 1180))
                    await self.human.jitter(260, 680)
        return rows

    async def _resolve_target_article(self, post_id: str) -> Any | None:
        article = await self._find_post_article_in_context(post_id, scan_rounds=1)
        if article:
            return article
        if not self.page:
            return None
        fallback = self.page.locator("article").first
        if await self._count_locator(fallback):
            return fallback
        return None

    async def _wait_for_post_disappearance(self, post_id: str, retries: int = 14) -> bool:
        for _ in range(max(3, retries)):
            if not self.page:
                return True
            current_url = self.page.url or ""
            article = await self._find_post_article_in_context(post_id, scan_rounds=1)
            if f"/status/{post_id}" not in current_url and article is None:
                return True
            if article is None:
                return True
            await asyncio.sleep(0.25)
        return False

    async def current_state(self) -> dict[str, str]:
        if not self.page:
            return {"state": "not_started", "url": ""}
        url = self.page.url or ""
        state: dict[str, str]
        if any(token in url for token in ("/login", "/i/flow/")) and await self._any_selector(ui.LOGIN_SELECTORS):
            state = {"state": "login", "url": url}
        elif "/compose/" in url:
            state = {"state": "compose", "url": url}
        elif self.STATUS_URL_RE.search(url):
            state = {"state": "status", "url": url}
        elif "/notifications" in url:
            state = {"state": "notifications", "url": url}
        elif "/search" in url:
            state = {"state": "search", "url": url}
        elif await self._looks_like_home_timeline():
            state = {"state": "home", "url": url}
        elif self.PROFILE_URL_RE.search(url):
            state = {"state": "profile", "url": url}
        else:
            state = {"state": "unknown", "url": url}
        if self.last_action_error:
            state["last_action_error"] = self.last_action_error.summary
        return state

    async def login_state(self) -> LoginState:
        if not self.page:
            return LoginState(
                logged_in=False,
                page_state="not_started",
                url="",
                browser_started=False,
                login_required=True,
                raw={"reason": "page_not_started"},
            )

        state = await self.current_state()
        page_state = str(state.get("state") or "unknown")
        url = str(state.get("url") or self.page.url or "")
        login_markers_visible = await self._any_selector(ui.LOGIN_SELECTORS)
        logged_in_selectors_visible = await self._any_selector(ui.LOGGED_IN_SELECTORS)
        login_link_visible = False
        with contextlib.suppress(Exception):
            login_link_visible = bool(await self._count_locator(self.page.locator('a[href="/login"]')))

        login_url = any(token in url for token in ("/login", "/i/flow/"))
        known_logged_in_surface = page_state in {"home", "search", "status", "profile", "notifications", "compose"}
        logged_in = bool(
            logged_in_selectors_visible
            or (known_logged_in_surface and not login_markers_visible and not login_link_visible and not login_url)
        )
        raw: dict[str, Any] = {
            "login_markers_visible": login_markers_visible,
            "logged_in_selectors_visible": logged_in_selectors_visible,
            "login_link_visible": login_link_visible,
        }
        if self.last_action_error:
            raw["last_action_error"] = self.last_action_error.to_dict()
        return LoginState(
            logged_in=logged_in,
            page_state=page_state,
            url=url,
            browser_started=True,
            active_home_tab=await self._active_home_tab(),
            login_required=not logged_in,
            raw=raw,
        )

    async def is_logged_in(self) -> bool:
        if not self.page:
            return False
        if not await self._open_home_via_click():
            await self._goto(f"{self.BASE_URL}/home")
        current_url = self.page.url or ""
        login_markers_visible = await self._any_selector(ui.LOGIN_SELECTORS)
        if login_markers_visible and any(token in current_url for token in ("/login", "/i/flow/")):
            return False
        if any(token in current_url for token in ("/login", "/i/flow/")) and not await self._looks_like_home_timeline():
            return False
        if await self._any_selector(ui.LOGGED_IN_SELECTORS):
            return True
        if await self._looks_like_home_timeline():
            return not login_markers_visible
        return not login_markers_visible and not await self._count_locator(self.page.locator('a[href="/login"]'))

    async def open_login_page(self) -> None:
        await self._goto(f"{self.BASE_URL}/i/flow/login")
        await self._wait_network_idle(1400)

    async def _collect_posts_from_current_page(
        self,
        limit: int,
        scroll_rounds: int,
        max_scan: int,
        stagnation_limit: int,
        allow_backtrack: bool = False,
    ) -> list[ObservedPostData]:
        if not self.page:
            return []
        posts: list[ObservedPostData] = []
        seen_ids: set[str] = set()
        articles = self.page.locator("article")
        stagnant_rounds = 0
        for round_idx in range(scroll_rounds):
            total = await self._count_locator(articles)
            new_posts_in_round = 0
            for idx in range(min(total, max_scan)):
                item = articles.nth(idx)
                link = item.locator('a[href*="/status/"]').first
                if not await self._count_locator(link):
                    continue
                href = (await self._get_attribute(link, "href")) or ""
                post_id = self._extract_post_id(href)
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                text = (await self._extract_article_text(item))[:4000]
                body = text
                with contextlib.suppress(Exception):
                    body = (await self._inner_text(item, timeout_ms=1200)).strip()[:4000]
                author = await self._extract_article_author_handle(item)
                author_display_name = await self._extract_article_author_display_name(item)
                created_at = await self._extract_article_timestamp(item)
                social_context = await self._extract_article_social_context(item)
                metrics = await self._extract_article_metrics(item)
                media = await self._extract_article_media(item)
                limit_state = self._extract_article_author_limit_state(body, social_context)
                posts.append(
                    ObservedPostData(
                        post_id,
                        author,
                        text,
                        {
                            "post_id": post_id,
                            "url": href,
                            "author_handle": author,
                            "author_display_name": author_display_name,
                            "created_at": created_at.isoformat() if created_at else None,
                            "text": text,
                            "body": body,
                            "social_context": social_context,
                            "media": media,
                            **limit_state,
                            **metrics,
                            "metrics": metrics,
                        },
                    )
                )
                new_posts_in_round += 1
                if len(posts) >= limit:
                    return posts
            if round_idx < scroll_rounds - 1:
                if new_posts_in_round == 0:
                    stagnant_rounds += 1
                else:
                    stagnant_rounds = 0
                if stagnant_rounds >= stagnation_limit:
                    break
                if allow_backtrack and round_idx % 4 == 3:
                    await self._random_scroll(random.randint(-180, -60), random.randint(420, 1200))
                else:
                    await self._random_scroll(random.randint(420, 1200))
                await self.human.jitter(300, 780)
        return posts

    async def read_timeline_detailed(
        self,
        limit: int = 20,
        tab: str = "for_you",
        force_refresh: bool = False,
        reset_scroll: bool = False,
    ) -> TimelineReadResult:
        limit = max(1, int(limit))
        requested_tab = str(tab or "for_you").strip().lower().replace("-", "_").replace(" ", "_")
        if requested_tab not in {"for_you", "following"}:
            requested_tab = "for_you"
        warnings: list[str] = []
        force_refreshed = False
        if force_refresh:
            if await self.return_home(force_refresh=True):
                force_refreshed = True
                if not await self._select_home_tab(requested_tab):
                    warnings.append("home_tab_select_failed")
            else:
                warnings.append("home_settle_failed")
        elif not await self.settle_home(requested_tab):
            warnings.append("home_settle_failed")
        if reset_scroll and self.page:
            await self._keyboard_press("Home")
            await self.human.jitter(180, 520)
        else:
            await self.human.jitter(120, 420)
        if not self.page:
            return TimelineReadResult(
                posts=[],
                requested_tab=requested_tab,
                active_tab="",
                source_url="",
                current_state="not_started",
                raw_count=0,
                article_count=0,
                force_refreshed=force_refreshed,
                reset_scroll=reset_scroll,
                warnings=["page_not_started"],
            )

        active_tab = await self._active_home_tab()
        if active_tab and active_tab != requested_tab:
            warnings.append(f"active_tab_mismatch:{active_tab}")
        article_count = await self._count_locator(self.page.locator("article"))
        first_pass = await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(4, (limit // 4) + 6),
            max_scan=max(limit * 3, 40),
            stagnation_limit=3,
            allow_backtrack=True,
        )
        if first_pass:
            return TimelineReadResult(
                posts=first_pass,
                requested_tab=requested_tab,
                active_tab=active_tab,
                source_url=self.page.url or "",
                current_state=await self._current_state_name(),
                raw_count=len(first_pass),
                article_count=article_count,
                force_refreshed=force_refreshed,
                reset_scroll=reset_scroll,
                warnings=warnings,
            )

        # Retry once after forcing home navigation; timeline occasionally renders late.
        await self._goto(f"{self.BASE_URL}/home")
        await self.human.jitter(220, 620)
        await self._select_home_tab(requested_tab)
        posts = await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(5, (limit // 4) + 7),
            max_scan=max(limit * 3, 45),
            stagnation_limit=3,
            allow_backtrack=True,
        )
        article_count = await self._count_locator(self.page.locator("article")) if self.page else 0
        active_tab = await self._active_home_tab()
        if not posts:
            warnings.append("timeline_empty_after_retry")
        return TimelineReadResult(
            posts=posts,
            requested_tab=requested_tab,
            active_tab=active_tab,
            source_url=self.page.url if self.page else "",
            current_state=await self._current_state_name(),
            raw_count=len(posts),
            article_count=article_count,
            force_refreshed=force_refreshed,
            reset_scroll=reset_scroll,
            warnings=warnings,
        )

    async def read_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        result = await self.read_timeline_detailed(limit=limit)
        return result.posts

    async def read_following_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        result = await self.read_timeline_detailed(limit=limit, tab="following")
        return result.posts

    async def read_visible_posts(self, limit: int = 20) -> list[ObservedPostData]:
        limit = max(1, int(limit))
        if not self.page:
            return []
        return await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(2, (limit // 5) + 2),
            max_scan=max(limit * 3, 30),
            stagnation_limit=2,
            allow_backtrack=False,
        )

    async def _notification_article_unread(self, article: Any) -> bool:
        if not article:
            return False
        for selector in ui.NOTIFICATION_UNREAD_SELECTORS:
            with contextlib.suppress(Exception):
                marker = article.locator(selector).first
                if await self._count_locator(marker):
                    return True
        with contextlib.suppress(Exception):
            aria = (await self._get_attribute(article, "aria-label")) or ""
            if re.search(r"\bunread\b", aria, flags=re.IGNORECASE):
                return True
        return False

    async def _extract_notification_actor_handle(self, article: Any) -> str:
        if not article:
            return ""
        links = article.locator('a[href^="/"]')
        with contextlib.suppress(Exception):
            for idx in range(min(await self._count_locator(links), 12)):
                href = (await self._get_attribute(links.nth(idx), "href")) or ""
                handle = self._extract_profile_handle_from_href(href)
                if handle:
                    return handle
        with contextlib.suppress(Exception):
            return self._extract_handle_from_text(await self._inner_text(article, timeout_ms=1200))
        return ""

    def _classify_notification_type(self, text: str, social_context: str = "") -> str:
        raw = f"{social_context}\n{text}".lower()
        if "followed you" in raw:
            return "follow"
        if "mentioned you" in raw or "mention" in raw:
            return "mention"
        if "replied" in raw or "replying to" in raw:
            return "reply"
        if "quoted" in raw:
            return "quote"
        if "reposted" in raw or "retweeted" in raw:
            return "repost"
        if "liked" in raw:
            return "like"
        return "notification"

    def _notification_id(
        self,
        post_id: str,
        actor: str,
        notification_type: str,
        created_at: datetime | None,
        text: str,
    ) -> str:
        if post_id:
            return f"{notification_type}:{post_id}:{actor or '-'}"
        fingerprint = "|".join(
            [
                actor,
                notification_type,
                created_at.isoformat() if created_at else "",
                text[:600],
            ]
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"{notification_type}:{digest}"

    async def _extract_notification_from_article(self, article: Any) -> ObservedNotificationData | None:
        if not article:
            return None

        status_urls = await self._extract_article_status_urls(article)
        url = status_urls[0] if status_urls else ""
        post_id = self._extract_post_id(url)
        status_post_ids = self._extract_status_ids_from_urls(status_urls)
        related_post_ids = [item for item in status_post_ids if item != post_id]
        text = (await self._extract_article_text(article))[:4000]
        body = text
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()[:4000]
        social_context = await self._extract_article_social_context(article)
        actor = await self._extract_notification_actor_handle(article)
        if not actor:
            actor = await self._extract_article_author_handle(article)
        actor_display_name = await self._extract_article_author_display_name(article)
        created_at = await self._extract_article_timestamp(article)
        unread = await self._notification_article_unread(article)
        metrics = await self._extract_article_metrics(article)
        media = await self._extract_article_media(article)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        notification_type = self._classify_notification_type(body or text, social_context)
        notification_id = self._notification_id(post_id, actor, notification_type, created_at, body or text)

        return ObservedNotificationData(
            notification_id=notification_id,
            notification_type=notification_type,
            actor=actor,
            text=text or body,
            raw={
                "post_id": post_id,
                "platform_post_id": post_id,
                "url": url,
                "status_urls": status_urls,
                "status_post_ids": status_post_ids,
                "related_post_ids": related_post_ids,
                "actor_handle": actor,
                "actor_display_name": actor_display_name,
                "created_at": created_at.isoformat() if created_at else None,
                "unread": unread,
                "social_context": social_context,
                "body": body,
                "media": media,
                **limit_state,
                **metrics,
                "metrics": metrics,
            },
        )

    async def _collect_notifications_from_current_page(
        self,
        limit: int,
        unread_only: bool = False,
        scroll_rounds: int = 6,
        max_scan: int = 80,
        stagnation_limit: int = 2,
    ) -> list[ObservedNotificationData]:
        if not self.page:
            return []

        notifications: list[ObservedNotificationData] = []
        seen_ids: set[str] = set()
        stagnant_rounds = 0
        for round_idx in range(max(1, int(scroll_rounds))):
            articles = self.page.locator('article[data-testid="tweet"], article')
            total = await self._count_locator(articles)
            new_items = 0
            for idx in range(min(total, max(1, int(max_scan)))):
                notification = await self._extract_notification_from_article(articles.nth(idx))
                if not notification:
                    continue
                if notification.notification_id in seen_ids:
                    continue
                seen_ids.add(notification.notification_id)
                new_items += 1
                if unread_only and not notification.unread:
                    continue
                notifications.append(notification)
                if len(notifications) >= limit:
                    return notifications

            if round_idx < scroll_rounds - 1:
                if new_items == 0:
                    stagnant_rounds += 1
                else:
                    stagnant_rounds = 0
                if stagnant_rounds >= stagnation_limit:
                    break
                await self._random_scroll(random.randint(420, 1180))
                await self.human.jitter(260, 720)

        return notifications

    async def read_notifications(
        self,
        limit: int = 20,
        unread_only: bool = False,
    ) -> list[ObservedNotificationData]:
        if not self.page:
            return []

        max_items = max(1, min(int(limit), 400))
        if not await self._looks_like_notifications_page():
            if not await self._open_notifications_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/notifications")
        if not await self._looks_like_notifications_page():
            return []

        return await self._collect_notifications_from_current_page(
            limit=max_items,
            unread_only=unread_only,
            scroll_rounds=max(2, (max_items // 5) + 3),
            max_scan=max(max_items * 4, 40),
            stagnation_limit=2,
        )

    def _parse_iso_datetime(self, value: str | None) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        text = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _extract_article_timestamp(self, article: Any) -> datetime | None:
        if not article:
            return None
        try:
            time_node = article.locator("time").first
            if not await self._count_locator(time_node):
                return None
            stamp = await self._get_attribute(time_node, "datetime")
            return self._parse_iso_datetime(stamp)
        except Exception:
            return None

    async def read_mentions(
        self,
        account_handle: str,
        hours_back: int = 2,
        limit: int = 120,
        min_scroll_rounds: int = 8,
        max_scroll_rounds: int = 36,
    ) -> list[ObservedPostData]:
        if not self.page:
            return []
        handle = self._normalize_username(account_handle).lower()
        if not handle:
            return []

        if not await self._looks_like_notifications_page():
            if not await self._open_notifications_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/notifications")
        if not await self._looks_like_notifications_page():
            return []

        await self._open_notifications_mentions_tab()

        max_items = max(5, min(int(limit), 400))
        min_rounds = max(2, min(int(min_scroll_rounds), 100))
        max_rounds = max(min_rounds, min(int(max_scroll_rounds), 140))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, min(int(hours_back), 72)))

        mentions: list[ObservedPostData] = []
        seen_ids: set[str] = set()
        saw_older_than_cutoff = False
        consecutive_old_rounds = 0
        stagnant_rounds = 0

        for round_idx in range(max_rounds):
            articles = self.page.locator('article[data-testid="tweet"], article')
            total = await self._count_locator(articles)
            new_mentions = 0
            old_in_round = False

            for idx in range(min(total, 120)):
                item = articles.nth(idx)
                link = item.locator('a[href*="/status/"]').first
                if not await self._count_locator(link):
                    continue
                href = (await self._get_attribute(link, "href")) or ""
                post_id = self._extract_post_id(href)
                if not post_id or post_id in seen_ids:
                    continue

                text = (await self._extract_article_text(item))[:4000]
                if f"@{handle}" not in text.lower():
                    continue
                body = text
                with contextlib.suppress(Exception):
                    body = (await self._inner_text(item, timeout_ms=1200)).strip()[:4000]

                author = ""
                auth_locator = item.locator('div[data-testid="User-Name"] a[href^="/"]').first
                if await self._count_locator(auth_locator):
                    auth_href = (await self._get_attribute(auth_locator, "href")) or ""
                    author = auth_href.strip("/").split("/")[-1]
                if author and author.lower() == handle:
                    continue

                created_at = await self._extract_article_timestamp(item)
                if created_at and created_at <= cutoff:
                    old_in_round = True

                metrics = await self._extract_article_metrics(item)
                limit_state = self._extract_article_author_limit_state(body)
                mentions.append(
                    ObservedPostData(
                        post_id,
                        author,
                        text,
                        {
                            "url": href,
                            "mentioned_handle": handle,
                            "created_at": created_at.isoformat() if created_at else None,
                            "body": body,
                            **limit_state,
                            **metrics,
                            "metrics": metrics,
                        },
                    )
                )
                seen_ids.add(post_id)
                new_mentions += 1
                if len(mentions) >= max_items:
                    return mentions

            if old_in_round:
                saw_older_than_cutoff = True
                consecutive_old_rounds += 1
            else:
                consecutive_old_rounds = 0

            if new_mentions == 0:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            rounds_done = round_idx + 1
            if rounds_done >= min_rounds and saw_older_than_cutoff and consecutive_old_rounds >= 2:
                break
            if rounds_done >= min_rounds and saw_older_than_cutoff and stagnant_rounds >= 4:
                break

            if round_idx < max_rounds - 1:
                await self._random_scroll(random.randint(460, 1280))
                await self.human.jitter(260, 720)

        return mentions

    async def search_posts(self, query: str, limit: int = 10) -> list[ObservedPostData]:
        limit = max(1, int(limit))
        if not self.page:
            return []

        # Click-first flow: open search UI, type, and switch tabs.
        if await self._open_search_query(query):
            await self._set_search_mode("live")
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts
            await self._set_search_mode("top")
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts

        # URL fallback only if UI-driven flow fails.
        search_urls = [
            f"{self.BASE_URL}/search?q={quote_plus(query)}&f=live",
            f"{self.BASE_URL}/search?q={quote_plus(query)}&f=top",
        ]
        for idx, url in enumerate(search_urls, start=1):
            try:
                await self._goto(url)
            except Exception as exc:
                logger.warning("search_navigation_failed url=%s error=%s", url, str(exc)[:260])
                continue
            posts = await self._collect_posts_from_current_page(
                limit=limit,
                scroll_rounds=max(3, (limit // 4) + 4),
                max_scan=max(limit * 3, 35),
                stagnation_limit=2,
                allow_backtrack=False,
            )
            if posts:
                return posts
            logger.info("search_empty_result query=%s mode=%s", query, "live" if idx == 1 else "top")
        return []

    def _parse_count_token(self, raw: str) -> int:
        token = (raw or "").strip().lower().replace(",", "").replace(" ", "")
        if not token:
            return 0
        mult = 1
        if token.endswith("k"):
            mult = 1_000
            token = token[:-1]
        elif token.endswith("m"):
            mult = 1_000_000
            token = token[:-1]
        elif token.endswith("b"):
            mult = 1_000_000_000
            token = token[:-1]
        try:
            return int(float(token) * mult)
        except Exception:
            return 0

    def _extract_metric_token(self, text: str, pattern: str) -> int:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            return 0
        return self._parse_count_token(match.group(1))

    def _zero_metrics(self) -> dict[str, int]:
        return {"views": 0, "likes": 0, "replies": 0, "comments": 0, "reposts": 0, "follows": 0}

    def _extract_metrics_from_text(self, text: str) -> dict[str, int]:
        metrics = self._zero_metrics()
        raw = str(text or "")
        metrics["views"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+views?\b")
        metrics["likes"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+likes?\b")
        metrics["replies"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+repl(?:y|ies)\b")
        metrics["comments"] = metrics["replies"]
        metrics["reposts"] = self._extract_metric_token(raw, r"([\d.,]+[kmb]?)\s+(?:reposts?|retweets?)\b")
        return metrics

    def _line_metric_label(self, line: str) -> str | None:
        normalized = re.sub(r"[^a-z]", "", str(line or "").lower())
        return {
            "reply": "replies",
            "replies": "replies",
            "comment": "comments",
            "comments": "comments",
            "repost": "reposts",
            "reposts": "reposts",
            "retweet": "reposts",
            "retweets": "reposts",
            "like": "likes",
            "likes": "likes",
            "view": "views",
            "views": "views",
        }.get(normalized)

    def _article_metric_region(self, body: str, article_text: str) -> str:
        raw_body = str(body or "")
        raw_text = str(article_text or "").strip()
        if not raw_body:
            return ""
        if not raw_text:
            return raw_body[-800:]

        body_lines = [line.strip() for line in raw_body.splitlines()]
        text_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not text_lines:
            return raw_body[-800:]
        lowered_text = [line.lower() for line in text_lines]
        lowered_body = [line.lower() for line in body_lines]
        window = len(lowered_text)
        for idx in range(0, max(0, len(lowered_body) - window + 1)):
            if lowered_body[idx : idx + window] == lowered_text:
                return "\n".join(body_lines[idx + window :])
        if len(raw_text) >= 12 and raw_text in raw_body:
            return raw_body.split(raw_text, 1)[1]
        return ""

    def _extract_metrics_from_article_region(self, region: str) -> dict[str, int]:
        metrics = self._extract_metrics_from_text(region)
        lines = [line.strip() for line in str(region or "").splitlines() if line.strip()]
        consumed: set[int] = set()
        for idx, line in enumerate(lines):
            if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?", line):
                continue
            value = self._parse_count_token(line)
            if not 0 <= value < 5_000_000_000:
                continue
            label = self._line_metric_label(lines[idx + 1]) if idx + 1 < len(lines) else None
            label_idx = idx + 1 if label else None
            if not label and idx > 0:
                label = self._line_metric_label(lines[idx - 1])
                label_idx = idx - 1 if label else None
            if not label:
                continue
            key = "replies" if label == "comments" else label
            metrics[key] = max(metrics.get(key, 0), value)
            if key == "replies":
                metrics["comments"] = metrics["replies"]
            consumed.add(idx)
            if label_idx is not None:
                consumed.add(label_idx)

        numeric_values_reversed: list[int] = []
        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx]
            if idx in consumed or self._line_metric_label(line):
                break
            if re.fullmatch(r"\d+\s?[smhd]", line):
                break
            if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?", line):
                break
            value = self._parse_count_token(line)
            if 0 <= value < 5_000_000_000:
                numeric_values_reversed.append(value)
        numeric_values = list(reversed(numeric_values_reversed))
        if len(numeric_values) >= 4:
            replies, reposts, likes, views = numeric_values[-4:]
            metrics["replies"] = max(metrics["replies"], replies)
            metrics["comments"] = metrics["replies"]
            metrics["reposts"] = max(metrics["reposts"], reposts)
            metrics["likes"] = max(metrics["likes"], likes)
            metrics["views"] = max(metrics["views"], views)
        elif len(numeric_values) == 3:
            reposts, likes, views = numeric_values[-3:]
            metrics["reposts"] = max(metrics["reposts"], reposts)
            metrics["likes"] = max(metrics["likes"], likes)
            metrics["views"] = max(metrics["views"], views)
        return metrics

    async def _extract_article_metrics(self, article: Any) -> dict[str, int]:
        metrics = self._zero_metrics()
        if not article:
            return metrics
        try:
            body = await self._inner_text(article)
            article_text = await self._extract_article_text(article)
            metric_region = self._article_metric_region(body, article_text)
            parsed = self._extract_metrics_from_article_region(metric_region)
            for key in metrics:
                metrics[key] = max(metrics[key], int(parsed.get(key) or 0))
        except Exception:
            pass

        # Pull aria-label text from known metric buttons as a fallback.
        selector_map = {
            "replies": 'button[data-testid="reply"], [data-testid="reply"]',
            "reposts": 'button[data-testid="retweet"], button[data-testid="unretweet"], [data-testid="retweet"], [data-testid="unretweet"]',
            "likes": 'button[data-testid="like"], button[data-testid="unlike"], [data-testid="like"], [data-testid="unlike"]',
            "views": 'a[href$="/analytics"], a[aria-label*="View post analytics"], a[aria-label*="Views"], [aria-label*="Views"]',
        }
        for key, selector in selector_map.items():
            try:
                locators = article.locator(selector)
                total = await self._count_locator(locators)
                if not total:
                    continue
                for idx in range(min(total, 12)):
                    locator = locators.nth(idx)
                    aria = (await self._get_attribute(locator, "aria-label")) or ""
                    if not aria:
                        aria = await self._inner_text(locator)
                    if not aria:
                        continue
                    token = self._extract_metric_token(aria, r"([\d.,]+[kmb]?)")
                    if token > 0:
                        metrics[key] = max(metrics[key], token)
            except Exception:
                continue
        metrics["comments"] = metrics["replies"]
        return metrics

    def _extract_labeled_count(self, text: str, labels: Sequence[str]) -> tuple[int, str] | None:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if not compact:
            return None
        token = r"(\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?)"
        for label in labels:
            escaped = re.escape(label)
            for pattern in (
                rf"{token}\s+{escaped}\b",
                rf"\b{escaped}\b\s+{token}",
            ):
                match = re.search(pattern, compact, flags=re.IGNORECASE)
                if match:
                    return self._parse_count_token(match.group(1)), match.group(0)
        return None

    def _extract_first_count(self, text: str) -> tuple[int, str] | None:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        match = re.search(r"\b(\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?)\b", compact)
        if not match:
            return None
        return self._parse_count_token(match.group(1)), match.group(1)

    def _link_path_matches(self, href: str, endings: Sequence[str]) -> bool:
        try:
            path = urlparse(str(href or "")).path.rstrip("/").lower()
        except Exception:
            return False
        return any(path.endswith(f"/{ending.strip('/').lower()}") for ending in endings)

    def _extract_account_count(
        self,
        payload: dict[str, Any],
        *,
        labels: Sequence[str],
        link_endings: Sequence[str] = (),
        allow_text_fallback: bool = True,
    ) -> tuple[int, str] | None:
        links = payload.get("links")
        if link_endings and isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href") or "")
                if not self._link_path_matches(href, link_endings):
                    continue
                text = " ".join(
                    str(item.get(key) or "")
                    for key in ("text", "aria_label", "title")
                    if str(item.get(key) or "").strip()
                )
                found = self._extract_labeled_count(text, labels) or self._extract_first_count(text)
                if found is not None:
                    return found[0], f"link:{href or '-'}:{found[1]}"

        if not allow_text_fallback:
            return None

        text_candidates = [
            *[str(value or "") for value in payload.get("user_name_blocks", []) if isinstance(value, str)],
            str(payload.get("profile_text") or ""),
            str(payload.get("meta_description") or ""),
            str(payload.get("title") or ""),
        ]
        for text in text_candidates:
            found = self._extract_labeled_count(text, labels)
            if found is not None:
                return found[0], f"text:{found[1]}"
        return None

    def _profile_title_identity(self, title: str) -> tuple[str, str]:
        match = self.PROFILE_TITLE_RE.search(str(title or "").strip())
        if not match:
            return "", ""
        return match.group(1).strip(), self._normalize_account_handle(match.group(2))

    def _profile_identity_from_payload(self, requested_handle: str, payload: dict[str, Any]) -> tuple[str, str]:
        title_display, title_handle = self._profile_title_identity(str(payload.get("title") or ""))
        url_handle = self._profile_handle_from_url(str(payload.get("current_url") or ""))
        resolved_handle = title_handle or url_handle or requested_handle
        display_name = ""
        blocks = payload.get("user_name_blocks")
        if isinstance(blocks, list):
            for block in blocks:
                lines = [line.strip() for line in str(block or "").splitlines() if line.strip()]
                if not lines:
                    continue
                handle_index = -1
                for idx, line in enumerate(lines):
                    if self._normalize_account_handle(line).lower() == resolved_handle.lower():
                        handle_index = idx
                        break
                if handle_index > 0:
                    display_name = lines[handle_index - 1]
                    break
                for line in lines:
                    lowered = line.lower()
                    if line.startswith("@") or lowered.endswith(" posts") or lowered in {"posts", "replies", "media", "likes"}:
                        continue
                    if self._extract_labeled_count(line, ("post", "posts", "follower", "followers", "following")):
                        continue
                    display_name = line
                    break
                if display_name:
                    break
        if not display_name:
            display_name = title_display
        return resolved_handle, display_name

    def _account_stats_from_payload(
        self,
        requested_handle: str,
        payload: dict[str, Any],
        captured_at: str,
        raw_base: dict[str, Any] | None = None,
    ) -> AccountStats:
        raw: dict[str, Any] = dict(raw_base or {})
        warnings = list(raw.get("warnings") or [])
        raw["warnings"] = warnings
        raw["dom"] = payload
        raw["current_url"] = str(payload.get("current_url") or raw.get("current_url") or "")

        handle, display_name = self._profile_identity_from_payload(requested_handle, payload)
        if not handle:
            handle = requested_handle
            warnings.append("profile_handle_unavailable")
        profile_url = f"{self.BASE_URL}/{handle}" if handle else str(payload.get("current_url") or "")

        count_specs = {
            "followers": {"labels": ("followers", "follower"), "link_endings": ("followers", "verified_followers"), "allow_text_fallback": True},
            "following": {"labels": ("following",), "link_endings": ("following",), "allow_text_fallback": True},
            "posts": {"labels": ("posts", "post"), "link_endings": (), "allow_text_fallback": True},
            "likes": {"labels": ("likes", "like"), "link_endings": ("likes",), "allow_text_fallback": False},
            "media": {"labels": ("media",), "link_endings": ("media",), "allow_text_fallback": False},
        }
        counts: dict[str, int] = {}
        count_sources: dict[str, str] = {}
        for key, spec in count_specs.items():
            found = self._extract_account_count(
                payload,
                labels=spec["labels"],  # type: ignore[arg-type]
                link_endings=spec["link_endings"],  # type: ignore[arg-type]
                allow_text_fallback=bool(spec["allow_text_fallback"]),
            )
            if found is None:
                counts[key] = 0
                warnings.append(f"{key}_count_unavailable")
            else:
                counts[key], count_sources[key] = found
        raw["count_sources"] = count_sources

        verified_value = payload.get("verified")
        verified = bool(verified_value) if verified_value is not None else None
        if verified is None:
            warnings.append("verified_state_unavailable")

        return AccountStats(
            handle=handle,
            display_name=display_name,
            profile_url=profile_url,
            followers=counts["followers"],
            following=counts["following"],
            posts=counts["posts"],
            likes=counts["likes"],
            media=counts["media"],
            verified=verified,
            bio=str(payload.get("bio") or ""),
            location=str(payload.get("location") or ""),
            joined_at=str(payload.get("joined_at") or ""),
            captured_at=captured_at,
            raw=raw,
        )

    def _account_stats_dom_script(self) -> str:
        return """() => {
            const compact = value => String(value || '').replace(/\\s+/g, ' ').trim();
            const visibleText = node => {
                if (!node) return '';
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (style && (style.display === 'none' || style.visibility === 'hidden')) return '';
                if (rect && rect.width === 0 && rect.height === 0) return '';
                return compact(node.innerText || node.textContent || '');
            };
            const root = document.querySelector('main [data-testid="primaryColumn"]') || document.querySelector('main') || document.body;
            const firstText = selectors => {
                for (const selector of selectors) {
                    const node = root.querySelector(selector) || document.querySelector(selector);
                    const text = visibleText(node);
                    if (text) return text;
                }
                return '';
            };
            const links = Array.from(root.querySelectorAll('a[href]')).slice(0, 240).map(link => ({
                href: link.href || link.getAttribute('href') || '',
                text: visibleText(link),
                aria_label: link.getAttribute('aria-label') || '',
                title: link.getAttribute('title') || ''
            }));
            const userNameBlocks = Array.from(root.querySelectorAll('[data-testid="UserName"]'))
                .slice(0, 12)
                .map(visibleText)
                .filter(Boolean);
            const verifiedNode = root.querySelector(
                '[data-testid="icon-verified"], svg[aria-label*="Verified"], [aria-label*="Verified account"], [aria-label*="Blue verified"]'
            );
            return {
                current_url: window.location.href,
                title: document.title || '',
                meta_description: document.querySelector('meta[name="description"]')?.getAttribute('content') || '',
                profile_text: visibleText(root).slice(0, 12000),
                user_name_blocks: userNameBlocks,
                bio: firstText(['[data-testid="UserDescription"]']),
                location: firstText(['[data-testid="UserLocation"]']),
                joined_at: firstText(['[data-testid="UserJoinDate"]']),
                verified: Boolean(verifiedNode),
                links
            };
        }"""

    async def _profile_payload_from_page(self, page: Any | None) -> dict[str, Any]:
        value = await self._page_evaluate(page, self._account_stats_dom_script())
        return value if isinstance(value, dict) else {}

    def _account_stats_error(
        self,
        handle: str,
        captured_at: str,
        reason: str,
        raw: dict[str, Any] | None = None,
    ) -> AccountStats:
        details = dict(raw or {})
        warnings = list(details.get("warnings") or [])
        warnings.append(reason)
        details["warnings"] = warnings
        details["error"] = reason
        return AccountStats(handle=handle, profile_url=f"{self.BASE_URL}/{handle}" if handle else "", captured_at=captured_at, raw=details)

    async def account_stats(self, handle: str | None = None) -> AccountStats:
        captured_at = datetime.now(timezone.utc).isoformat()
        requested_handle = self._normalize_account_handle(handle)
        raw: dict[str, Any] = {
            "requested_handle": handle or "",
            "normalized_requested_handle": requested_handle,
            "warnings": [],
        }

        if handle is not None and not requested_handle:
            return self._account_stats_error("", captured_at, "invalid_handle", raw)

        if not self.page:
            return self._account_stats_error(requested_handle, captured_at, "page_not_started", raw)

        target_handle = requested_handle
        if not target_handle:
            detected = await self._get_authenticated_handle()
            target_handle = self._normalize_account_handle(detected)
            raw["detected_authenticated_handle"] = detected or ""
        if not target_handle:
            current_handle = self._profile_handle_from_url(await self._page_url(self.page))
            target_handle = current_handle
            if current_handle:
                raw["warnings"].append("authenticated_handle_unavailable_used_current_profile")
        if not target_handle:
            return self._account_stats_error("", captured_at, "account_handle_unavailable", raw)

        target_url = f"{self.BASE_URL}/{target_handle}"
        raw["target_url"] = target_url

        stats_page = None
        temporary_page = False
        active_page_used = False
        active_previous_url = await self._page_url(self.page)
        payload: dict[str, Any] = {}
        try:
            stats_page = await self._new_context_page()
            temporary_page = stats_page is not None
            if stats_page is None:
                raw["warnings"].append("temporary_page_unavailable_used_active_page")
                active_page_used = True
                await self._goto(target_url)
                stats_page = self.page
            else:
                await self._page_goto(stats_page, target_url)
            payload = await self._profile_payload_from_page(stats_page)
            if not payload:
                return self._account_stats_error(target_handle, captured_at, "profile_payload_unavailable", raw)
        except Exception as exc:
            if self._is_profile_in_use_error(exc):
                raise RuntimeError("profile_in_use") from exc
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            raw["exception_type"] = type(exc).__name__
            raw["exception"] = str(exc)[:400]
            return self._account_stats_error(target_handle, captured_at, "profile_stats_capture_failed", raw)
        finally:
            if temporary_page:
                await self._close_context_page(stats_page)
            elif active_page_used and active_previous_url and active_previous_url != target_url and self.page:
                with contextlib.suppress(Exception):
                    await self._goto(active_previous_url)
                    raw["restored_url"] = active_previous_url

        return self._account_stats_from_payload(target_handle, payload, captured_at, raw)

    async def profile_recent_metrics(
        self,
        username: str,
        limit: int = 40,
        source: str | None = None,
    ) -> list[dict[str, int | str | bool]]:
        if not self.page:
            return []
        handle = self._normalize_username(username)
        if not handle:
            return []
        source_filter = str(source or "").strip().lower()
        if source_filter in {"reply", "replies"}:
            source_filter = "with_replies"
        if source_filter and source_filter not in {"posts", "with_replies"}:
            source_filter = ""
        rows: list[dict[str, int | str | bool]] = []
        seen: set[str] = set()
        max_rows = max(5, min(limit, 3000))
        row_cap = max_rows * 2
        endpoints = [
            (f"{self.BASE_URL}/{handle}", "posts"),
            (f"{self.BASE_URL}/{handle}/with_replies", "with_replies"),
        ]
        if source_filter:
            endpoints = [item for item in endpoints if item[1] == source_filter]
        try:
            for endpoint, source in endpoints:
                await self._goto(endpoint)
                scroll_rounds = max(2, (max_rows // 6) + 2)
                for round_idx in range(scroll_rounds):
                    articles = self.page.locator("article")
                    total = await self._count_locator(articles)
                    for idx in range(min(total, max(max_rows * 2, 50))):
                        article = articles.nth(idx)
                        status_link = article.locator('a[href*="/status/"]').first
                        if not await self._count_locator(status_link):
                            continue
                        href = (await self._get_attribute(status_link, "href")) or ""
                        post_id = self._extract_post_id(href)
                        if not post_id or post_id in seen:
                            continue
                        seen.add(post_id)
                        item = await self._classify_profile_article(article, handle)
                        text = str(item.get("text") or (await self._extract_article_text(article)))[:900]
                        metrics = await self._extract_article_metrics(article)
                        rows.append(
                            {
                                "post_id": post_id,
                                "url": str(item.get("url") or href or f"{self.BASE_URL}/i/web/status/{post_id}"),
                                "author": str(item.get("author") or ""),
                                "is_reply": bool(item.get("is_reply")),
                                "is_repost": bool(item.get("is_repost")),
                                "is_quote": bool(item.get("is_quote")),
                                "likes": int(metrics.get("likes") or 0),
                                "replies": int(metrics.get("replies") or 0),
                                "comments": int(metrics.get("comments") or 0),
                                "reposts": int(metrics.get("reposts") or 0),
                                "views": int(metrics.get("views") or 0),
                                "text": text,
                                "source": source,
                            }
                        )
                        if len(rows) >= row_cap:
                            return rows
                    if round_idx < scroll_rounds - 1:
                        await self._random_scroll(random.randint(320, 1080))
                        await self.human.jitter(240, 580)
            return rows
        finally:
            await self._return_home()

    async def _extract_article_text(self, article: Any) -> str:
        tweet_text = article.locator('div[data-testid="tweetText"]').first
        if await self._count_locator(tweet_text):
            with contextlib.suppress(Exception):
                return (await self._inner_text(tweet_text, timeout_ms=1200)).strip()
        with contextlib.suppress(Exception):
            return (await self._inner_text(article, timeout_ms=1200)).strip()
        return ""

    async def _article_summary(self, article: Any, *, target_post_id: str = "", thread_index: int | None = None) -> dict[str, Any]:
        url = await self._extract_article_status_url(article)
        post_id = self._extract_post_id(url)
        text = (await self._extract_article_text(article))[:1200]
        body = text
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()[:3000]
        social_context = await self._extract_article_social_context(article)
        created_at = await self._extract_article_timestamp(article)
        metrics = await self._extract_article_metrics(article)
        limit_state = self._extract_article_author_limit_state(body, social_context)
        row = {
            "post_id": post_id,
            "url": url,
            "author": await self._extract_article_author_handle(article),
            "author_display_name": await self._extract_article_author_display_name(article),
            "text": text,
            "body": body,
            "created_at": created_at.isoformat() if created_at else None,
            "social_context": social_context,
            "metrics": metrics,
            "media": await self._extract_article_media(article),
            **limit_state,
        }
        if target_post_id:
            row["is_target"] = post_id == target_post_id
        if thread_index is not None:
            row["thread_index"] = thread_index
        return row

    async def preflight_action(
        self,
        platform_post_id: str,
        action: str = "reply",
        *,
        open_composer: bool = False,
    ) -> ActionPreflight:
        post_id = self._normalize_post_id(platform_post_id)
        action_name = str(action or "").strip().lower()
        if action_name not in {"reply", "quote", "like"}:
            action_name = "reply"
        if not self.page:
            return ActionPreflight(False, action_name, post_id, reason="page_not_started", current_state="not_started")
        if not post_id:
            return ActionPreflight(False, action_name, "", reason="target_post_not_found", current_url=self.page.url or "")

        article = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if article is None:
            with contextlib.suppress(Exception):
                await self._open_post_page(post_id)
                article = await self._find_post_article_in_context(post_id, scan_rounds=1)

        result = ActionPreflight(
            ok=False,
            action=action_name,
            target_post_id=post_id,
            target_url=f"{self.BASE_URL}/i/web/status/{post_id}",
            current_url=self.page.url or "",
            current_state=await self._current_state_name(),
            active_home_tab=await self._active_home_tab(),
            article_found=article is not None,
        )
        if article is None:
            result.reason = "target_post_not_found"
            return result

        summary = await self._article_summary(article, target_post_id=post_id)
        result.raw = {"article": summary}
        result.author_limited = bool(summary.get("author_limited"))
        result.reply_limited = bool(summary.get("reply_limited"))
        result.author_limit_notice = str(summary.get("author_limit_notice") or "")
        result.quote_limited = False

        selectors = {
            "reply": ui.COMMENT_BUTTONS,
            "quote": ui.REPOST_BUTTONS,
            "like": ui.LIKE_BUTTONS,
        }[action_name]
        button = await self._find_first_in_scope(article, selectors, timeout_ms=900, require_enabled=False)
        result.button_found = button is not None
        if button is not None:
            result.button_enabled = await self._is_button_enabled(button)
        if action_name == "reply" and result.reply_limited:
            result.reason = "reply_limited"
            return result
        if not result.button_found:
            result.reason = f"{action_name}_button_not_found"
            return result
        if not result.button_enabled:
            result.reason = "submit_disabled"
            return result
        if open_composer and action_name in {"reply", "quote"}:
            box = await (
                self._open_reply_box_in_current_context(post_id)
                if action_name == "reply"
                else self._open_quote_box_in_current_context(post_id)
            )
            result.composer_opened = box is not None
            if box is not None:
                submit = await self._find_reply_submit_button(box, timeout_ms=900)
                result.submit_available = submit is not None
            await self._dismiss_reply_ui()
            if not result.composer_opened:
                result.reason = "composer_not_opened"
                return result
        result.ok = True
        return result

    async def attach_images_preflight(self, image_paths: ImagePathInput | None) -> MediaPreflight:
        candidates: list[ImagePath]
        if image_paths is None:
            candidates = []
        elif isinstance(image_paths, (str, os.PathLike)):
            candidates = [image_paths]
        else:
            candidates = list(image_paths)
        errors: list[dict[str, Any]] = []
        normalized: list[str] = []
        if len(candidates) > ui.MAX_IMAGES_PER_POST:
            errors.append({"reason": "too_many_images", "max": ui.MAX_IMAGES_PER_POST, "count": len(candidates)})
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            else:
                path = path.resolve()
            item = {"path": str(path), "exists": path.is_file(), "supported_extension": path.suffix.lower() in ui.SUPPORTED_IMAGE_EXTENSIONS}
            if not path.is_file():
                item["reason"] = "file_not_found"
                errors.append(item)
                continue
            if path.suffix.lower() not in ui.SUPPORTED_IMAGE_EXTENSIONS:
                item["reason"] = "unsupported_extension"
                errors.append(item)
                continue
            item["size_bytes"] = path.stat().st_size
            normalized.append(str(path))
        upload_input_found = False
        if self.page:
            with contextlib.suppress(Exception):
                upload_input_found = await self._find_media_input_for_composer(None, timeout_ms=300) is not None
        return MediaPreflight(
            ok=not errors,
            normalized_paths=normalized,
            file_count=len(candidates),
            max_file_count=ui.MAX_IMAGES_PER_POST,
            errors=errors,
            raw={"upload_input_found": upload_input_found},
        )

    async def debug_snapshot(self, output_dir: str | os.PathLike[str], article_limit: int = 12) -> dict[str, Any]:
        out_dir = Path(output_dir).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.cwd() / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / "page.html"
        screenshot_path = out_dir / "page.png"
        manifest_path = out_dir / "manifest.json"

        page_html = await self._page_content()
        html_path.write_text(page_html, encoding="utf-8")
        with contextlib.suppress(Exception):
            await self._screenshot(str(screenshot_path))

        articles_summary: list[dict[str, Any]] = []
        article_count = 0
        if self.page:
            articles = self.page.locator("article")
            article_count = await self._count_locator(articles)
            for idx in range(min(article_count, max(0, int(article_limit)))):
                with contextlib.suppress(Exception):
                    articles_summary.append(await self._article_summary(articles.nth(idx), thread_index=idx))

        selector_probe = {
            "reply_button": bool(self.page and await self._find_first(ui.COMMENT_BUTTONS, timeout_ms=200)),
            "quote_button": bool(self.page and await self._find_first(ui.REPOST_BUTTONS, timeout_ms=200)),
            "like_button": bool(self.page and await self._find_first(ui.LIKE_BUTTONS, timeout_ms=200)),
            "submit_button": bool(self.page and await self._find_first(ui.POST_BUTTONS, timeout_ms=200)),
            "media_input": bool(self.page and await self._find_media_input_for_composer(None, timeout_ms=200)),
        }
        manifest = {
            "url": self.page.url if self.page else "",
            "current_state": await self._current_state_name(),
            "login_state": "logged_in" if self.page and await self._any_selector(ui.LOGGED_IN_SELECTORS) else "login_required" if self.page else "not_started",
            "active_home_tab": await self._active_home_tab(),
            "article_count": article_count,
            "articles": articles_summary,
            "visible_dialogs": await self._visible_dialogs(),
            "composer_state": {"open": await self._is_compose_state()},
            "selector_probe": selector_probe,
            "screenshot_path": str(screenshot_path),
            "html_path": str(html_path),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return {"manifest_path": str(manifest_path), **manifest}

    async def capture_post_media(
        self,
        platform_post_id: str,
        output_dir: str | os.PathLike[str],
        frame_count: int = 3,
    ) -> list[MediaCaptureData]:
        target_id = self._normalize_post_id(platform_post_id)
        if not target_id or not self.page:
            return []
        out_dir = Path(output_dir).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.cwd() / out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if not await self._open_post_page(target_id):
            return []
        article = await self._resolve_target_article(target_id)
        if article is None:
            return []
        captures = await self._capture_article_media_nodes(article, target_id, out_dir, frame_count=frame_count)
        return [item for item in captures if item.path]

    async def _visible_dialogs(self) -> list[str]:
        if not self.page:
            return []
        try:
            value = await self._evaluate(
                """() => Array.from(document.querySelectorAll('[role="dialog"]'))
                    .filter(node => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    })
                    .map(node => String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 240))"""
            )
            return [str(item) for item in value] if isinstance(value, list) else []
        except Exception:
            return []

    async def health_check(self) -> ControllerHealth:
        browser_started = self.page is not None
        state = await self.login_state()
        content = (await self._page_content()).lower() if browser_started else ""
        return ControllerHealth(
            browser_started=browser_started,
            logged_in=state.logged_in,
            current_url=state.url,
            current_state=state.page_state,
            active_home_tab=state.active_home_tab,
            login_required=state.login_required,
            account_locked="account is locked" in content or "temporarily restricted" in content,
            rate_limited="rate limit" in content or "try again later" in content,
            blocking_modal_present=bool(await self._visible_dialogs()),
            last_action_error=self.last_action_error.to_dict() if self.last_action_error else {},
        )

    async def read_post_thread_context(
        self,
        post_id: str,
        limit: int = 6,
        include_parent: bool = True,
        include_target: bool = True,
        include_replies: bool = True,
    ) -> list[ObservedPostData]:
        target_id = self._normalize_post_id(post_id)
        if not target_id or not await self._open_post_page(target_id) or not self.page:
            return []
        for _ in range(3):
            articles_probe = self.page.locator("article")
            if await self._count_locator(articles_probe):
                break
            with contextlib.suppress(Exception):
                await self.page.wait_for_selector("article", timeout=1800)
            await self.human.jitter(250, 650)
        summaries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        target_seen = False
        max_rows = max(1, int(limit)) * 4
        for scan_attempt in range(4):
            articles = self.page.locator("article")
            total = await self._count_locator(articles)
            for idx in range(min(total, max_rows)):
                article = articles.nth(idx)
                summary = await self._article_summary(article, target_post_id=target_id, thread_index=idx)
                item_id = str(summary.get("post_id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                summaries.append(summary)
                if item_id == target_id:
                    target_seen = True
            if target_seen or scan_attempt >= 3:
                break
            await self._random_scroll(850)
            await self.human.jitter(220, 620)
            await self._wait_network_idle(900)
        rows: list[ObservedPostData] = []
        for summary in summaries:
            item_id = str(summary.get("post_id") or "")
            if not item_id:
                continue
            is_target = item_id == target_id
            if is_target and not include_target:
                continue
            if not is_target and not (include_parent or include_replies):
                continue
            rows.append(
                ObservedPostData(
                    platform_post_id=item_id,
                    author=str(summary.get("author") or ""),
                    text=str(summary.get("text") or ""),
                    raw={**summary, "conversation_id": target_id},
                )
            )
        max_limit = max(1, int(limit))
        if len(rows) > max_limit:
            target_index = next((idx for idx, row in enumerate(rows) if row.platform_post_id == target_id), -1)
            if target_index >= 0:
                start = max(0, target_index - max_limit + 1)
                rows = rows[start : target_index + 1]
            else:
                rows = rows[-max_limit:]
        return rows

    async def _open_compose_box(self):
        # Prefer click-driven compose from home/sidebar.
        if not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                with contextlib.suppress(Exception):
                    await self._goto(f"{self.BASE_URL}/home")
        compose_btn = await self._find_first(ui.COMPOSE_BUTTONS, timeout_ms=2000)
        if compose_btn:
            try:
                await self._click(compose_btn)
                await self.human.jitter(350, 1000)
                box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=2600)
                if box:
                    return box
            except Exception:
                pass

        # URL fallback only if click flow fails.
        compose_urls = [f"{self.BASE_URL}/compose/tweet", f"{self.BASE_URL}/compose/post"]
        for url in compose_urls:
            try:
                await self._goto(url)
            except Exception as exc:
                logger.warning("open_compose_navigation_failed url=%s error=%s", url, str(exc)[:260])
                continue
            box = await self._find_first(ui.COMPOSE_TEXTBOXES, timeout_ms=2200)
            if box:
                return box
        return None

    async def _clear_textbox(self, box: Any) -> None:
        try:
            await self._click(box)
            await self._keyboard_press("Control+A")
            await self._keyboard_press("Backspace")
            await self.human.jitter(70, 220)
        except Exception:
            pass

    async def _post_from_compose(self, text: str, image_paths: list[str]) -> str | None:
        if not self.page:
            return None
        box = await self._open_compose_box()
        if not box:
            logger.warning("post_text_compose_box_not_found")
            return None
        await self._move_mouse()
        await self._clear_textbox(box)
        await self._type_text(box, text)
        if not await self._attach_images_to_composer(box, image_paths):
            logger.warning("post_text_media_attach_failed count=%s", len(image_paths))
            return None
        await self.human.jitter(280, 820)
        submitted = await self._submit_post()
        if not submitted:
            logger.warning("post_text_submit_failed_after_retries")
            return None
        await self.human.jitter(900, 1700)
        post_id = await self._find_recent_own_created_post_id("post", text)
        if not post_id:
            post_id = await self._guess_recent_post_id()
        if not post_id:
            logger.info("post_text_submitted_unknown_post_id")
        return post_id

    async def post_text_detailed(self, text: str, image_paths: ImagePathInput | None = None) -> ActionResult:
        if not self.page:
            return await self._action_result("post", failure_reason="page_not_started", failure_stage="not_started")
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "post",
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage="media_attach",
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        post_id = await self._post_from_compose(text, media_preflight.normalized_paths)
        if post_id:
            result = await self._action_result("post", ok=True, created_post_id=post_id, media_paths=media_preflight.normalized_paths)
            result.composer_opened = True
            result.submit_clicked = True
            result.confirmation_observed = True
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        return await self._action_result(
            "post",
            failure_reason="submit_not_confirmed",
            failure_stage="post_submit",
            media_paths=media_preflight.normalized_paths,
        )

    async def post_text(self, text: str, image_paths: ImagePathInput | None = None) -> str | None:
        result = await self.post_text_detailed(text, image_paths=image_paths)
        return result.created_post_id if result.ok else None

    async def post_image(self, image_paths: ImagePathInput, text: str = "") -> str | None:
        warnings.warn(
            "post_image() is deprecated; use post_text(text, image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = self._normalize_image_paths(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self._post_from_compose(text, image_files)

    async def engage_post(
        self,
        platform_post_id: str,
        do_view: bool = True,
        do_like: bool = False,
        dwell_seconds: tuple[int, int] = (3, 8),
        return_to_previous: bool = True,
    ) -> dict[str, bool]:
        result = {"opened": False, "viewed": False, "liked": False}
        if not await self._open_post_page(platform_post_id):
            return result
        result["opened"] = True
        try:
            if do_view:
                min_s = max(1, int(dwell_seconds[0]))
                max_s = max(min_s, int(dwell_seconds[1]))
                await self.human.jitter(min_s * 1000, max_s * 1000)
                await self._random_scroll(random.randint(110, 360), random.randint(-70, 160))
                await self.human.jitter(550, 1500)
                result["viewed"] = True
            if do_like:
                btn = await self._find_first(ui.LIKE_BUTTONS, timeout_ms=900)
                if btn:
                    await self._click(btn)
                    await self.human.jitter(500, 1400)
                    result["liked"] = True
                else:
                    # Idempotent success when already liked.
                    result["liked"] = await self._find_first(ui.LIKE_ACTIVE_BUTTONS) is not None
            return result
        except Exception as exc:
            logger.warning("engage_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return result
        finally:
            if return_to_previous:
                back_ok = False
                before_url = self.page.url if self.page else ""
                with contextlib.suppress(Exception):
                    await self._go_back()
                    after_url = self.page.url if self.page else ""
                    back_ok = bool(after_url and after_url != before_url)
                    if not back_ok and await self._looks_like_home_timeline():
                        back_ok = True
                if not back_ok:
                    await self._return_home()
            else:
                await self._return_home()

    async def like_post_detailed(self, platform_post_id: str) -> ActionResult:
        if not self.page:
            return await self._action_result("like", target_post_id=str(platform_post_id), failure_reason="page_not_started", failure_stage="not_started")
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return await self._action_result("like", target_post_id=str(platform_post_id), failure_reason="target_post_not_found", failure_stage="target_lookup")
        try:
            if await self._like_in_current_context(platform_post_id):
                result = await self._action_result("like", ok=True, target_post_id=post_id)
                result.confirmation_observed = True
                return result
            fallback = await self.engage_post(post_id, do_view=False, do_like=True)
            if fallback.get("liked"):
                result = await self._action_result("like", ok=True, target_post_id=post_id, raw={"fallback": fallback})
                result.submit_clicked = True
                result.confirmation_observed = True
                return result
            if fallback.get("opened"):
                return await self._action_result(
                    "like",
                    target_post_id=post_id,
                    failure_reason="like_button_not_found",
                    failure_stage="action_control",
                    raw={"fallback": fallback},
                )
            preflight = await self.preflight_action(post_id, action="like")
            logger.info("like_inline_not_found target=%s page=%s", platform_post_id, self.page.url or "")
            return await self._action_result(
                "like",
                target_post_id=post_id,
                failure_reason=preflight.reason or "target_post_not_found",
                failure_stage="target_lookup",
                raw={"preflight": preflight.to_dict()},
            )
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("like_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return await self._action_result(
                "like",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage="unknown",
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def like_post(self, platform_post_id: str) -> bool:
        result = await self.like_post_detailed(platform_post_id)
        return result.ok

    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        result = await self.engage_post(platform_post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        return bool(result.get("viewed"))

    async def view_post_detailed(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> ActionResult:
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("view", target_post_id=post_id, failure_reason="page_not_started", failure_stage="not_started")
        result = await self.engage_post(post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        if result.get("viewed"):
            return await self._action_result("view", ok=True, target_post_id=post_id)
        return await self._action_result("view", target_post_id=post_id, failure_reason="target_post_not_found", failure_stage="target_lookup")

    async def quote_post(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        image_files = self._normalize_image_paths(image_paths)
        try:
            if not await self._open_post_page(post_id):
                return None
            box = await self._open_quote_box_in_current_context(post_id)
            if not box:
                logger.warning("quote_composer_not_found target=%s; falling back to status-url post", post_id)
                quote_url = f"{self.BASE_URL}/i/web/status/{post_id}"
                body = f"{text.rstrip()}\n{quote_url}" if text.strip() else quote_url
                return await self._post_from_compose(body, image_files)

            await self._move_mouse()
            await self._clear_textbox(box)
            await self._type_text(box, text)
            if not await self._attach_images_to_composer(box, image_files):
                logger.warning("quote_media_attach_failed target=%s count=%s", post_id, len(image_files))
                return None
            await self.human.jitter(280, 820)
            submitted = await self._submit_post()
            if not submitted:
                logger.warning("quote_submit_failed_after_retries target=%s", post_id)
                return None
            await self.human.jitter(900, 1700)
            quote_id = await self._find_recent_own_created_post_id("quote", text, target_post_id=post_id)
            if not quote_id:
                quote_id = await self._guess_recent_post_id()
            if quote_id and quote_id != post_id:
                return quote_id
            return "unknown_quote_id"
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("quote_post_failed target=%s error=%s", post_id, str(exc)[:260])
            return None

    async def quote_post_detailed(
        self,
        platform_post_id: str,
        text: str = "",
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("quote", target_post_id=post_id, failure_reason="page_not_started", failure_stage="not_started")
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "quote",
                target_post_id=post_id,
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage="media_attach",
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        preflight = await self.preflight_action(post_id, action="quote")
        created_id = await self.quote_post(post_id, text=text, image_paths=media_preflight.normalized_paths)
        if created_id:
            result = await self._action_result(
                "quote",
                ok=True,
                target_post_id=post_id,
                created_post_id=created_id,
                media_paths=media_preflight.normalized_paths,
                raw={"preflight": preflight.to_dict()},
            )
            result.composer_opened = True
            result.submit_clicked = True
            result.confirmation_observed = True
            result.media_attached = bool(media_preflight.normalized_paths)
            return result
        return await self._action_result(
            "quote",
            target_post_id=post_id,
            failure_reason=preflight.reason or "quote_button_or_box_not_found",
            failure_stage="composer_open",
            media_paths=media_preflight.normalized_paths,
            raw={"preflight": preflight.to_dict()},
        )

    async def quote_post_with_image(
        self,
        platform_post_id: str,
        image_paths: ImagePathInput,
        text: str = "",
    ) -> str | None:
        warnings.warn(
            "quote_post_with_image() is deprecated; use quote_post(..., image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = self._normalize_image_paths(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self.quote_post(platform_post_id, text=text, image_paths=image_files)

    async def reply_to_post_detailed(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> ActionResult:
        if not self.page:
            return await self._action_result("reply", target_post_id=str(platform_post_id), failure_reason="page_not_started", failure_stage="not_started")
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return await self._action_result("reply", target_post_id=str(platform_post_id), failure_reason="target_post_not_found", failure_stage="target_lookup")
        media_preflight = await self.attach_images_preflight(image_paths)
        if not media_preflight.ok:
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason=str(media_preflight.errors[0].get("reason") or "media_preflight_failed"),
                failure_stage="media_attach",
                media_paths=media_preflight.normalized_paths,
                diagnostic={"media_preflight": media_preflight.to_dict()},
            )
        image_files = media_preflight.normalized_paths
        try:
            for attempt in range(1, 4):
                preflight = await self.preflight_action(post_id, action="reply")
                if not preflight.ok and preflight.reason == "reply_limited":
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="reply_limited",
                        failure_stage="preflight",
                        attempts=attempt,
                        media_paths=image_files,
                        raw={"preflight": preflight.to_dict()},
                    )
                box = await self._open_reply_box_in_current_context(post_id, scan_rounds=min(4, attempt + 1))
                if not box:
                    logger.warning("reply_button_or_box_not_found target=%s attempt=%s", post_id, attempt)
                    await self._dismiss_reply_ui()
                    await self._random_scroll(random.randint(-120, 260))
                    await self.human.jitter(160, 460)
                    continue

                try:
                    await self._clear_textbox(box)
                    await self._type_text(box, text)
                except Exception as exc:
                    logger.warning("reply_text_entry_failed target=%s attempt=%s error=%s", post_id, attempt, str(exc)[:260])
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="text_entry_failed",
                        failure_stage="text_entry",
                        attempts=attempt,
                        media_paths=image_files,
                    )
                if not await self._attach_images_to_composer(box, image_files):
                    logger.warning(
                        "reply_media_attach_failed target=%s attempt=%s count=%s",
                        post_id,
                        attempt,
                        len(image_files),
                    )
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="media_upload_failed",
                        failure_stage="media_attach",
                        attempts=attempt,
                        media_paths=image_files,
                    )
                send = await self._find_reply_submit_button(box, timeout_ms=1800)
                submitted = False
                if send:
                    try:
                        await self._click(send)
                        await self.human.jitter(500, 1100)
                        submitted = True
                    except Exception as exc:
                        logger.warning("reply_send_click_failed target=%s attempt=%s error=%s", post_id, attempt, str(exc)[:260])
                if not submitted:
                    for shortcut in ("Control+Enter", "Meta+Enter"):
                        try:
                            await self._keyboard_press(shortcut)
                            await self.human.jitter(280, 720)
                            submitted = True
                            break
                        except Exception:
                            continue
                if not submitted:
                    logger.warning("reply_submit_trigger_not_found target=%s attempt=%s", post_id, attempt)
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="reply_submit_trigger_not_found",
                        failure_stage="submit_lookup",
                        attempts=attempt,
                        media_paths=image_files,
                    )

                await self.human.jitter(220, 620)
                if await self._has_reply_audience_modal():
                    audience_confirmed_post_submit = await self._confirm_reply_audience_if_needed()
                    if not audience_confirmed_post_submit:
                        logger.warning(
                            "reply_submit_blocked_by_audience_modal target=%s attempt=%s",
                            post_id,
                            attempt,
                        )
                        await self._dismiss_reply_ui()
                        return await self._action_result(
                            "reply",
                            target_post_id=post_id,
                            failure_reason="submit_blocked_by_audience_modal",
                            failure_stage="confirmation",
                            attempts=attempt,
                            media_paths=image_files,
                        )
                    await self.human.jitter(280, 760)
                    send_retry = await self._find_reply_submit_button(box, timeout_ms=1200)
                    if send_retry:
                        with contextlib.suppress(Exception):
                            await self._click(send_retry)
                            await self.human.jitter(360, 920)
                if await self._has_reply_audience_modal():
                    logger.warning(
                        "reply_submit_blocked_by_audience_modal target=%s attempt=%s",
                        post_id,
                        attempt,
                    )
                    await self._dismiss_reply_ui()
                    return await self._action_result(
                        "reply",
                        target_post_id=post_id,
                        failure_reason="submit_blocked_by_audience_modal",
                        failure_stage="confirmation",
                        attempts=attempt,
                        media_paths=image_files,
                    )

                await self.human.jitter(700, 1500)
                reply_id = await self._find_recent_own_created_post_id("reply", text, target_post_id=post_id)
                if not reply_id:
                    reply_id = await self._guess_recent_post_id()
                if reply_id and reply_id != post_id:
                    result = await self._action_result(
                        "reply",
                        ok=True,
                        target_post_id=post_id,
                        created_post_id=reply_id,
                        attempts=attempt,
                        media_paths=image_files,
                    )
                    result.composer_opened = True
                    result.submit_clicked = True
                    result.confirmation_observed = True
                    result.media_attached = bool(image_files)
                    return result
                # Verify the inline reply control became disabled or the draft cleared.
                send_after = await self._find_first(ui.REPLY_SEND_BUTTONS, timeout_ms=900)
                if send_after and (not await self._is_button_enabled(send_after)):
                    result = await self._action_result(
                        "reply",
                        ok=True,
                        target_post_id=post_id,
                        created_post_id="unknown_reply_id",
                        attempts=attempt,
                        media_paths=image_files,
                    )
                    result.composer_opened = True
                    result.submit_clicked = True
                    result.confirmation_observed = True
                    result.media_attached = bool(image_files)
                    return result
                box_text = ""
                with contextlib.suppress(Exception):
                    box_text = (await self._inner_text(box)).strip()
                if not box_text:
                    result = await self._action_result(
                        "reply",
                        ok=True,
                        target_post_id=post_id,
                        created_post_id="unknown_reply_id",
                        attempts=attempt,
                        media_paths=image_files,
                    )
                    result.composer_opened = True
                    result.submit_clicked = True
                    result.confirmation_observed = True
                    result.media_attached = bool(image_files)
                    return result
                logger.warning(
                    "reply_submit_not_confirmed target=%s attempt=%s",
                    post_id,
                    attempt,
                )
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason="submit_not_confirmed",
                failure_stage="post_submit",
                attempts=3,
                media_paths=image_files,
            )
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("reply_action_failed target=%s error=%s", post_id, str(exc)[:260])
            return await self._action_result(
                "reply",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage="unknown",
                media_paths=image_files,
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def _reply_to_post_impl(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        result = await self.reply_to_post_detailed(platform_post_id, text, image_paths=image_paths)
        return result.created_post_id if result.ok else None

    async def reply_to_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_paths)

    async def reply_with_image(self, platform_post_id: str, image_paths: ImagePathInput, text: str = "") -> str | None:
        warnings.warn(
            "reply_with_image() is deprecated; use reply_to_post(..., image_paths=...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        image_files = self._normalize_image_paths(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_files)

    async def _open_article_menu(self, article: Any | None) -> bool:
        target = None
        if article is not None:
            target = await self._find_first_in_scope(
                article,
                ui.POST_MENU_BUTTONS,
                timeout_ms=1400,
                require_enabled=True,
            )
        if not target:
            target = await self._find_first_enabled(ui.POST_MENU_BUTTONS, timeout_ms=1400)
        if not target:
            logger.warning("open_article_menu_target_not_found")
            return False
        try:
            await self._click(target)
        except Exception:
            await self._click_force(target)
        await self.human.jitter(240, 680)
        await self._wait_network_idle(700)
        return True

    async def _delete_owned_article(
        self,
        article: Any,
        item: dict[str, Any],
        expected_kind: str,
        own_handle: str,
    ) -> bool:
        post_id = str(item.get("post_id") or "")
        if not post_id:
            return False
        author = str(item.get("author") or "").lower()
        if author != own_handle.lower():
            logger.warning(
                "delete_owned_item_author_mismatch target=%s expected=%s actual=%s",
                post_id,
                own_handle,
                author or "-",
            )
            return False
        if expected_kind == "reply" and not bool(item.get("is_reply")):
            logger.warning("delete_reply_target_not_reply target=%s", post_id)
            return False
        if expected_kind == "quote" and not bool(item.get("is_quote")):
            logger.warning("delete_quote_target_not_quote target=%s", post_id)
            return False
        if expected_kind == "post" and (
            bool(item.get("is_reply")) or bool(item.get("is_repost")) or bool(item.get("is_quote"))
        ):
            logger.warning("delete_post_target_not_plain_post target=%s", post_id)
            return False
        if not await self._open_article_menu(article):
            logger.warning("delete_owned_item_menu_open_failed target=%s", post_id)
            return False
        delete_item = await self._find_first(ui.DELETE_MENU_ITEMS, timeout_ms=2400)
        if not delete_item:
            logger.warning("delete_menu_item_not_found target=%s", post_id)
            return False
        await self._click(delete_item)
        await self.human.jitter(260, 760)
        confirm = await self._find_first_enabled(ui.DELETE_CONFIRM_BUTTONS, timeout_ms=2600)
        if not confirm:
            logger.warning("delete_confirm_button_not_found target=%s", post_id)
            return False
        await self._click(confirm)
        await self.human.jitter(500, 1300)
        await self._wait_network_idle(1200)
        return await self._wait_for_profile_article_disappearance(own_handle, post_id, expected_kind)

    async def _delete_owned_status_item(self, platform_post_id: str, expected_kind: str) -> bool:
        if not self.page:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("delete_owned_item_missing_authenticated_handle target=%s", post_id)
            return False
        direct = await self._find_owned_status_article_by_post_id(own_handle, post_id, expected_kind)
        if direct is not None:
            article, item = direct
            if await self._delete_owned_article(article, item, expected_kind, own_handle):
                return True
            logger.warning("direct_delete_status_failed target=%s kind=%s", post_id, expected_kind)
        article = await self._find_profile_article_by_post_id(own_handle, post_id, expected_kind)
        if article is None:
            logger.warning(
                "delete_owned_item_article_not_found target=%s kind=%s handle=%s",
                post_id,
                expected_kind,
                own_handle,
            )
            return False
        item = await self._classify_profile_article(article, own_handle)
        return await self._delete_owned_article(article, item, expected_kind, own_handle)

    async def _undo_repost_article(self, article: Any, post_id: str) -> bool:
        if not self.page:
            return False
        scope = article or self.page
        active_btn = await self._find_first_in_scope(
            scope,
            ui.REPOST_ACTIVE_BUTTONS,
            timeout_ms=1600,
            require_enabled=True,
        )
        if not active_btn:
            logger.warning("undo_repost_active_button_not_found target=%s", post_id)
            return False
        await self._click(active_btn)
        await self.human.jitter(220, 680)
        confirm = await self._find_first_enabled(ui.UNDO_REPOST_BUTTONS, timeout_ms=2200)
        if confirm:
            await self._click(confirm)
            await self.human.jitter(300, 900)
        await self._wait_network_idle(900)
        still_active = await self._find_first_in_scope(scope, ui.REPOST_ACTIVE_BUTTONS, timeout_ms=700)
        inactive_btn = await self._find_first_in_scope(scope, ui.REPOST_BUTTONS, timeout_ms=900, require_enabled=True)
        return inactive_btn is not None or still_active is None

    async def _undo_repost(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("undo_repost_missing_authenticated_handle target=%s", post_id)
            return False
        direct = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if direct is None:
            with contextlib.suppress(Exception):
                if await self._open_post_page(post_id):
                    direct = await self._find_post_article_in_context(post_id, scan_rounds=2)
        if direct is not None and await self._undo_repost_article(direct, post_id):
            return True
        article = await self._find_profile_article_by_post_id(own_handle, post_id, "repost")
        if article is None:
            logger.warning("undo_repost_article_not_found target=%s handle=%s", post_id, own_handle)
            return False
        return await self._undo_repost_article(article, post_id)

    async def _delete_all_profile_items(self, kind: str) -> list[str]:
        if not self.page:
            return []
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            logger.warning("delete_all_profile_items_missing_authenticated_handle kind=%s", kind)
            return []
        deleted_urls: list[str] = []
        attempted_ids: set[str] = set()
        visible_batch_size = 6
        while True:
            if not await self._open_profile_surface(own_handle, kind):
                return deleted_urls
            scan_limit = visible_batch_size
            candidates = await self._collect_visible_profile_candidates(
                own_handle,
                kind,
                limit=scan_limit,
                excluded_post_ids=attempted_ids,
            )
            if not candidates:
                snapshot = await self._scroll_snapshot()
                at_bottom_before_scroll = self._is_snapshot_at_bottom(snapshot)
                logger.info(
                    "delete_all_profile_items_no_visible_candidate kind=%s bottom=%s deleted=%s attempted=%s",
                    kind,
                    at_bottom_before_scroll,
                    len(deleted_urls),
                    len(attempted_ids),
                )
                if at_bottom_before_scroll:
                    return deleted_urls
                reached_bottom = await self._scroll_profile_surface_forward(kind)
                if reached_bottom:
                    logger.info(
                        "delete_all_profile_items_bottom_reached kind=%s deleted=%s attempted=%s",
                        kind,
                        len(deleted_urls),
                        len(attempted_ids),
                    )
                    return deleted_urls
                continue
            progress = False
            for article, item in candidates:
                post_id = str(item.get("post_id") or "")
                if not post_id:
                    continue
                attempted_ids.add(post_id)
                if kind == "repost":
                    ok = await self._undo_repost_article(article, post_id)
                else:
                    ok = await self._delete_owned_article(article, item, kind, own_handle)
                if ok:
                    deleted_urls.append(str(item.get("url") or f"{self.BASE_URL}/i/web/status/{post_id}"))
                    progress = True
                    break
                await self.human.jitter(280, 720)
            if not progress:
                return deleted_urls
            await self.human.jitter(420, 980)

    async def delete_post(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="post")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_reply(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="reply")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_reply_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_repost(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        try:
            return await self._undo_repost(platform_post_id)
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_repost_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_quote(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        try:
            return await self._delete_owned_status_item(platform_post_id, expected_kind="quote")
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("delete_quote_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False
        finally:
            with contextlib.suppress(Exception):
                await self._return_home()

    async def delete_all_posts(self) -> list[str]:
        return await self._delete_all_profile_items("post")

    async def delete_all_replies(self) -> list[str]:
        return await self._delete_all_profile_items("reply")

    async def delete_all_reposts(self) -> list[str]:
        return await self._delete_all_profile_items("repost")

    async def delete_all_quotes(self) -> list[str]:
        return await self._delete_all_profile_items("quote")

    async def delete_all_content(self) -> dict[str, list[str]]:
        return {
            "reposts": await self.delete_all_reposts(),
            "quotes": await self.delete_all_quotes(),
            "replies": await self.delete_all_replies(),
            "posts": await self.delete_all_posts(),
        }

    async def delete_post_detailed(self, platform_post_id: str, kind: str = "post") -> ActionResult:
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("delete", target_post_id=post_id, failure_reason="page_not_started", failure_stage="not_started")
        if kind == "reply":
            ok = await self.delete_reply(post_id)
        elif kind == "repost":
            ok = await self.delete_repost(post_id)
        elif kind == "quote":
            ok = await self.delete_quote(post_id)
        else:
            ok = await self.delete_post(post_id)
        return await self._action_result(
            "delete",
            ok=ok,
            target_post_id=post_id,
            failure_reason="" if ok else "target_post_not_found",
            failure_stage="confirmation" if ok else "target_lookup",
            raw={"kind": kind},
        )

    async def repost_post_detailed(self, platform_post_id: str) -> ActionResult:
        post_id = self._normalize_post_id(platform_post_id)
        if not self.page:
            return await self._action_result("repost", target_post_id=post_id, failure_reason="page_not_started", failure_stage="not_started")
        if not post_id or not await self._open_post_page(post_id):
            return await self._action_result("repost", target_post_id=post_id, failure_reason="target_post_not_found", failure_stage="target_lookup")
        try:
            repost_btn = await self._find_first_enabled(ui.REPOST_BUTTONS, timeout_ms=1400)
            if not repost_btn:
                return await self._action_result("repost", target_post_id=post_id, failure_reason="repost_button_not_found", failure_stage="target_lookup")
            await self._click(repost_btn)
            await self.human.jitter(220, 680)
            confirm = await self._find_first_enabled(
                ['[data-testid="retweetConfirm"]', '[role="menuitem"]:has-text("Repost")', 'button:has-text("Repost")'],
                timeout_ms=2200,
            )
            if confirm:
                await self._click(confirm)
                await self.human.jitter(400, 1100)
            result = await self._action_result("repost", ok=True, target_post_id=post_id)
            result.submit_clicked = True
            result.confirmation_observed = True
            return result
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            return await self._action_result(
                "repost",
                target_post_id=post_id,
                failure_reason="unknown_exception",
                failure_stage="unknown",
                diagnostic={"exception": type(exc).__name__, "message": str(exc)[:260]},
            )

    async def follow_user_detailed(self, username: str) -> ActionResult:
        handle = self._normalize_username(username)
        if not self.page:
            return await self._action_result("follow", failure_reason="page_not_started", failure_stage="not_started", raw={"username": handle})
        ok = await self.follow_user(handle)
        return await self._action_result(
            "follow",
            ok=ok,
            failure_reason="" if ok else (self.last_action_error.message if self.last_action_error else "target_user_not_found"),
            failure_stage="confirmation" if ok else "target_lookup",
            raw={"username": handle},
        )

    async def unfollow_user_detailed(self, username: str) -> ActionResult:
        handle = self._normalize_username(username)
        if not self.page:
            return await self._action_result("unfollow", failure_reason="page_not_started", failure_stage="not_started", raw={"username": handle})
        ok = await self.unfollow_user(handle)
        return await self._action_result(
            "unfollow",
            ok=ok,
            failure_reason="" if ok else (self.last_action_error.message if self.last_action_error else "target_user_not_found"),
            failure_stage="confirmation" if ok else "target_lookup",
            raw={"username": handle},
        )

    async def follow_user(self, username: str) -> bool:
        self._clear_action_error()
        handle = self._normalize_username(username)
        if not handle:
            return False
        if not await self._open_profile_page(handle):
            with contextlib.suppress(Exception):
                await self._goto(f"{self.BASE_URL}/{handle}")
        if not self.page:
            return False
        try:
            btn = await self._find_first([
                '[data-testid="userActions"] button:has-text("Follow")',
                'button:has-text("Follow")',
            ])
            if not btn:
                return False
            await self._click(btn)
            await self.human.jitter(900, 1700)
            return True
        except Exception as exc:
            self._handle_soft_ui_error("follow_user", exc, selector="button:has-text('Follow')")
            return False
        finally:
            await self._return_home()

    async def unfollow_user(self, username: str) -> bool:
        self._clear_action_error()
        handle = self._normalize_username(username)
        if not handle:
            return False
        if not await self._open_profile_page(handle):
            with contextlib.suppress(Exception):
                await self._goto(f"{self.BASE_URL}/{handle}")
        if not self.page:
            return False
        try:
            btn = await self._find_first([
                '[data-testid="userActions"] button:has-text("Following")',
                'button:has-text("Following")',
                '[role="button"]:has-text("Unfollow")',
            ])
            if not btn:
                return False
            await self._click(btn)
            confirm = await self._find_first(['button:has-text("Unfollow")'])
            if confirm:
                await self._click(confirm)
            await self.human.jitter(700, 1500)
            return True
        except Exception as exc:
            self._handle_soft_ui_error("unfollow_user", exc, selector="button:has-text('Unfollow')")
            return False
        finally:
            await self._return_home()

    async def post_metrics(self, platform_post_id: str) -> dict[str, int]:
        if not await self._open_post_page(platform_post_id):
            return {}
        if not self.page:
            return {}
        try:
            article = await self._post_metrics_article(platform_post_id)
            if not article or not await self._count_locator(article):
                return {}
            metrics = self._zero_metrics()
            article_metrics = await self._extract_article_metrics(article)
            for key in metrics:
                metrics[key] = max(int(metrics.get(key) or 0), int(article_metrics.get(key) or 0))
            return metrics
        except Exception as exc:
            logger.warning("post_metrics_parse_failed target=%s error=%s", platform_post_id, str(exc)[:260])
        return {}

    async def _post_metrics_article(self, platform_post_id: str) -> Any | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        articles = self.page.locator("article")
        try:
            article_count = min(await self._count_locator(articles), 12)
        except Exception:
            article_count = 0
        for index in range(article_count):
            article = articles.nth(index)
            status_link = article.locator(f'a[href*="/status/{post_id}"]')
            if await self._count_locator(status_link):
                return article
        return articles.first if article_count else None

    async def _status_page_has_rendered_article(self, post_id: str) -> bool:
        if not self.page:
            return False
        articles = self.page.locator("article")
        try:
            article_count = min(await self._count_locator(articles), 12)
        except Exception:
            article_count = 0
        if article_count <= 0:
            return False
        for index in range(article_count):
            article = articles.nth(index)
            with contextlib.suppress(Exception):
                status_link = article.locator(f'a[href*="/status/{post_id}"]')
                if await self._count_locator(status_link):
                    return True
        current_url = self.page.url or ""
        return f"/status/{post_id}" in current_url

    async def _status_page_missing(self) -> bool:
        if not self.page:
            return False
        try:
            body = (await self._inner_text(self.page.locator("body"), timeout_ms=1000)).lower()
        except Exception:
            return False
        missing_markers = (
            "this page doesn't exist",
            "this page doesn\u2019t exist",
            "post is unavailable",
            "something went wrong",
        )
        return any(marker in body for marker in missing_markers)

    async def _wait_for_status_article(self, post_id: str, *, timeout_ms: int = 4500) -> bool:
        deadline = time.monotonic() + (max(500, timeout_ms) / 1000.0)
        while time.monotonic() < deadline:
            if await self._status_page_has_rendered_article(post_id):
                return True
            if await self._status_page_missing():
                return False
            await self.human.jitter(180, 420)
        return await self._status_page_has_rendered_article(post_id)

    async def _open_post_page(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        if not platform_post_id:
            return False
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return False

        # Click-first: open from visible status links when possible.
        for _ in range(3):
            link = self.page.locator(f'a[href*="/status/{post_id}"]').first
            if await self._count_locator(link):
                try:
                    await self._click(link)
                    await self.human.jitter(280, 820)
                    await self._wait_network_idle(1300)
                    current_url = self.page.url or ""
                    if self.STATUS_URL_RE.search(current_url) and await self._wait_for_status_article(post_id):
                        return True
                except Exception as ex:
                    if self._is_driver_connection_closed(ex):
                        raise RuntimeError("playwright_driver_connection_closed") from ex
                    if self._is_target_closed_error(ex):
                        raise RuntimeError("target_page_or_context_closed") from ex
                    logger.warning("open_post_click_failed target=%s error=%s", post_id, str(ex)[:260])
            await self._random_scroll(random.randint(250, 760))
            await self.human.jitter(160, 440)

        # URL fallback only if clickable path fails.
        candidate_urls = [
            f"{self.BASE_URL}/i/web/status/{post_id}",
            f"{self.BASE_URL}/i/status/{post_id}",
        ]
        for candidate in candidate_urls:
            try:
                await self._goto(candidate)
                current_url = self.page.url or ""
                if self.STATUS_URL_RE.search(current_url) and await self._wait_for_status_article(post_id):
                    return True
                content = await self._page_content()
                if f"/status/{post_id}" in content and await self._wait_for_status_article(post_id):
                    return True
            except Exception as ex:
                if self._is_driver_connection_closed(ex):
                    raise RuntimeError("playwright_driver_connection_closed") from ex
                if self._is_target_closed_error(ex):
                    raise RuntimeError("target_page_or_context_closed") from ex
                logger.warning("open_post_navigation_failed target=%s url=%s error=%s", post_id, candidate, str(ex)[:260])
        logger.warning("open_post_failed target=%s", post_id)
        return False

    def _extract_post_id(self, href: str) -> str:
        found = self.STATUS_URL_RE.search(href or "")
        return found.group(1) if found else ""

    def _compact_created_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    def _created_text_matches(self, expected: str, candidate: str) -> bool:
        expected_text = self._compact_created_text(expected)
        candidate_text = self._compact_created_text(candidate)
        if len(expected_text) < 12 or len(candidate_text) < 12:
            return False
        if expected_text in candidate_text or candidate_text in expected_text:
            return True
        prefix_len = min(90, len(expected_text), len(candidate_text))
        return prefix_len >= 28 and expected_text[:prefix_len] == candidate_text[:prefix_len]

    async def _find_recent_own_created_post_id(
        self,
        kind: str,
        text: str,
        *,
        target_post_id: str = "",
    ) -> str | None:
        if not self.page:
            return None
        own_handle = await self._get_authenticated_handle()
        if not own_handle:
            return None
        expected_kind = kind if kind in {"post", "reply", "quote"} else "post"
        previous_url = self.page.url or ""
        try:
            items = await self._collect_recent_owned_created_candidates(own_handle, expected_kind, limit=48)
            matched: list[dict[str, Any]] = []
            for item in items:
                post_id = str(item.get("post_id") or "")
                if not post_id or (target_post_id and post_id == target_post_id):
                    continue
                if self._created_text_matches(text, str(item.get("text") or "")):
                    matched.append(item)
            if matched:
                def _score(item: dict[str, Any]) -> tuple[int, int, int]:
                    kind_match = int(
                        (expected_kind == "reply" and bool(item.get("is_reply")))
                        or (expected_kind == "quote" and bool(item.get("is_quote")))
                        or (
                            expected_kind == "post"
                            and not bool(item.get("is_reply"))
                            and not bool(item.get("is_quote"))
                        )
                    )
                    surface_match = int(
                        (expected_kind == "reply" and item.get("source_surface") == "with_replies")
                        or (expected_kind in {"post", "quote"} and item.get("source_surface") == "posts")
                    )
                    return kind_match, surface_match, len(str(item.get("text") or ""))

                best = sorted(matched, key=_score, reverse=True)[0]
                logger.info(
                    "created_post_id_resolved_from_profile kind=%s post_id=%s source=%s is_reply=%s is_quote=%s",
                    expected_kind,
                    best.get("post_id"),
                    best.get("source_surface") or "",
                    bool(best.get("is_reply")),
                    bool(best.get("is_quote")),
                )
                return str(best.get("post_id") or "") or None
        except Exception as exc:
            logger.warning(
                "created_post_id_profile_resolve_failed kind=%s handle=%s error=%s",
                expected_kind,
                own_handle,
                str(exc)[:260],
            )
        finally:
            if previous_url and self.page and (self.page.url or "") != previous_url:
                with contextlib.suppress(Exception):
                    await self._goto(previous_url)
        return None

    async def _guess_recent_post_id(self, *, allow_page_content: bool = False) -> str | None:
        if not self.page:
            return None
        try:
            current = self.page.url
            match = self.STATUS_URL_RE.search(current or "")
            if match:
                return match.group(1)
            if not allow_page_content:
                return None
            text = await self._page_content()
            found = self.STATUS_URL_RE.search(text)
            return found.group(1) if found else None
        except Exception:
            return None


XController = XTextAdapter
