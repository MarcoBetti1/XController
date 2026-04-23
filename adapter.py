from __future__ import annotations

import logging
import os
import random
import re
import time
import contextlib
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from typing import Any, Callable, Sequence

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.sync_api import BrowserContext as SyncBrowserContext, Page as SyncPage, sync_playwright as sync_playwright

from . import _ui_selectors as ui
from ._diagnostics import ActionFailureInfo, UIActionError
from .base import ObservedNotificationData, ObservedPostData, SocialPlatformAdapter
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
                        "headless": False,
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
        # Windows asyncio subprocess handling is frequently unavailable in this runtime.
        # Prefer sync Playwright there to avoid repeated transport failures.
        return os.name == "nt"

    async def _start_sync_fallback(self) -> None:
        viewport = {
            "width": random.randint(self.settings.browser_width_min, self.settings.browser_width_max),
            "height": random.randint(self.settings.browser_height_min, self.settings.browser_height_max),
        }
        context_kwargs: dict[str, Any] = {
            "headless": False,
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

    async def recover_home(self, force_nav: bool = False) -> bool:
        return await self._return_home(force_nav=force_nav)

    async def refresh_home(self, force_nav: bool = False) -> bool:
        if not self.page:
            return False
        home_ready = await self._return_home(force_nav=force_nav)
        if not home_ready and not force_nav:
            home_ready = await self._return_home(force_nav=True)
        if not home_ready:
            return False
        try:
            if self._sync_mode:
                await self._run_sync(self._sync_page.reload, wait_until="domcontentloaded")
            else:
                await self.page.reload(wait_until="domcontentloaded")
            await self.human.jitter(260, 780)
            await self._wait_network_idle(1300)
        except Exception as exc:
            logger.warning("refresh_home_reload_failed error=%s", str(exc)[:260])
        return await self._looks_like_home_timeline()

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

    async def _extract_article_status_url(self, article: Any) -> str:
        if not article:
            return ""
        link = article.locator('a[href*="/status/"]').first
        if not await self._count_locator(link):
            return ""
        href = (await self._get_attribute(link, "href")) or ""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"{self.BASE_URL}{href}"
        return href

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

    async def _article_has_reply_context(self, article: Any) -> bool:
        if not article:
            return False
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
        with contextlib.suppress(Exception):
            status_links = article.locator('a[href*="/status/"]')
            if await self._count_locator(status_links) >= 2:
                return True
        return False

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
        reply_context = await self._article_has_reply_context(article)
        is_reply = bool(author_lower == own_handle_lower and ("replying to" in body_lower or reply_context))
        limit_state = self._extract_article_author_limit_state(body, social_context)
        return {
            "post_id": post_id,
            "url": url or (f"{self.BASE_URL}/i/web/status/{post_id}" if post_id else ""),
            "author": author,
            "text": text,
            "social_context": social_context,
            "is_reply": is_reply,
            "is_repost": is_repost,
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
        return not bool(item.get("is_reply")) and not bool(item.get("is_repost"))

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
                author = ""
                auth_locator = item.locator('div[data-testid="User-Name"] a[href^="/"]').first
                if await self._count_locator(auth_locator):
                    auth_href = (await self._get_attribute(auth_locator, "href")) or ""
                    author = auth_href.strip("/").split("/")[-1]
                metrics = await self._extract_article_metrics(item)
                limit_state = self._extract_article_author_limit_state(body)
                posts.append(
                    ObservedPostData(
                        post_id,
                        author,
                        text,
                        {
                            "url": href,
                            "body": body,
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

    async def read_timeline(self, limit: int = 20) -> list[ObservedPostData]:
        limit = max(1, int(limit))
        if not await self._looks_like_home_timeline():
            if not await self._open_home_via_click():
                await self._goto(f"{self.BASE_URL}/home")
        else:
            await self.human.jitter(120, 420)
        if not self.page:
            return []

        first_pass = await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(4, (limit // 4) + 6),
            max_scan=max(limit * 3, 40),
            stagnation_limit=3,
            allow_backtrack=True,
        )
        if first_pass:
            return first_pass

        # Retry once after forcing home navigation; timeline occasionally renders late.
        await self._goto(f"{self.BASE_URL}/home")
        await self.human.jitter(220, 620)
        return await self._collect_posts_from_current_page(
            limit=limit,
            scroll_rounds=max(5, (limit // 4) + 7),
            max_scan=max(limit * 3, 45),
            stagnation_limit=3,
            allow_backtrack=True,
        )

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

        url = await self._extract_article_status_url(article)
        post_id = self._extract_post_id(url)
        text = (await self._extract_article_text(article))[:4000]
        body = text
        with contextlib.suppress(Exception):
            body = (await self._inner_text(article, timeout_ms=1200)).strip()[:4000]
        social_context = await self._extract_article_social_context(article)
        actor = await self._extract_notification_actor_handle(article)
        if not actor:
            actor = await self._extract_article_author_handle(article)
        created_at = await self._extract_article_timestamp(article)
        unread = await self._notification_article_unread(article)
        metrics = await self._extract_article_metrics(article)
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
                "created_at": created_at.isoformat() if created_at else None,
                "unread": unread,
                "social_context": social_context,
                "body": body,
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

    async def read_unread_notifications(self, limit: int = 20) -> list[ObservedNotificationData]:
        return await self.read_notifications(limit=limit, unread_only=True)

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
        token = (raw or "").strip().lower().replace(",", "")
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

    async def _extract_article_metrics(self, article: Any) -> dict[str, int]:
        metrics = self._zero_metrics()
        if not article:
            return metrics
        try:
            body = await self._inner_text(article)
            parsed = self._extract_metrics_from_text(body)
            for key in metrics:
                metrics[key] = max(metrics[key], int(parsed.get(key) or 0))
        except Exception:
            pass

        # Pull aria-label text from known metric buttons as a fallback.
        selector_map = {
            "replies": '[data-testid="reply"]',
            "reposts": '[data-testid="retweet"]',
            "likes": '[data-testid="like"], [data-testid="unlike"]',
            "views": '[data-testid="app-text-transition-container"]',
        }
        for key, selector in selector_map.items():
            try:
                locator = article.locator(selector).first
                if not await self._count_locator(locator):
                    continue
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

    async def profile_recent_metrics(self, username: str, limit: int = 40) -> list[dict[str, int | str]]:
        if not self.page:
            return []
        handle = self._normalize_username(username)
        if not handle:
            return []
        rows: list[dict[str, int | str]] = []
        seen: set[str] = set()
        max_rows = max(5, min(limit, 300))
        row_cap = max_rows * 2
        endpoints = [
            (f"{self.BASE_URL}/{handle}", "posts"),
            (f"{self.BASE_URL}/{handle}/with_replies", "with_replies"),
        ]
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
                        text = (await self._extract_article_text(article))[:900]
                        metrics = await self._extract_article_metrics(article)
                        rows.append(
                            {
                                "post_id": post_id,
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
        post_id = await self._guess_recent_post_id()
        if not post_id:
            logger.info("post_text_submitted_unknown_post_id")
        return post_id

    async def post_text(self, text: str, image_paths: ImagePathInput | None = None) -> str | None:
        image_files = self._normalize_image_paths(image_paths)
        return await self._post_from_compose(text, image_files)

    async def post_image(self, image_paths: ImagePathInput, text: str = "") -> str | None:
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

    async def like_post(self, platform_post_id: str) -> bool:
        if not self.page:
            return False
        try:
            if await self._like_in_current_context(platform_post_id):
                return True
            logger.info("like_inline_not_found target=%s page=%s", platform_post_id, self.page.url or "")
            return False
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("like_post_failed target=%s error=%s", platform_post_id, str(exc)[:260])
            return False

    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        result = await self.engage_post(platform_post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        return bool(result.get("viewed"))

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

    async def quote_post_with_image(
        self,
        platform_post_id: str,
        image_paths: ImagePathInput,
        text: str = "",
    ) -> str | None:
        image_files = self._normalize_image_paths(image_paths)
        if not image_files:
            raise ValueError("At least one image path is required")
        return await self.quote_post(platform_post_id, text=text, image_paths=image_files)

    async def _reply_to_post_impl(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        if not self.page:
            return None
        post_id = self._normalize_post_id(platform_post_id)
        if not post_id:
            return None
        image_files = self._normalize_image_paths(image_paths)
        try:
            for attempt in range(1, 4):
                box = await self._open_reply_box_in_current_context(post_id, scan_rounds=min(4, attempt + 1))
                if not box:
                    logger.warning("reply_button_or_box_not_found target=%s attempt=%s", post_id, attempt)
                    await self._dismiss_reply_ui()
                    await self._random_scroll(random.randint(-120, 260))
                    await self.human.jitter(160, 460)
                    continue

                await self._clear_textbox(box)
                await self._type_text(box, text)
                if not await self._attach_images_to_composer(box, image_files):
                    logger.warning(
                        "reply_media_attach_failed target=%s attempt=%s count=%s",
                        post_id,
                        attempt,
                        len(image_files),
                    )
                    await self._dismiss_reply_ui()
                    continue
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
                    continue

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
                        continue
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
                    continue

                await self.human.jitter(700, 1500)
                reply_id = await self._guess_recent_post_id()
                if reply_id and reply_id != post_id:
                    return reply_id
                # Verify the inline reply control became disabled or the draft cleared.
                send_after = await self._find_first(ui.REPLY_SEND_BUTTONS, timeout_ms=900)
                if send_after and (not await self._is_button_enabled(send_after)):
                    return "unknown_reply_id"
                box_text = ""
                with contextlib.suppress(Exception):
                    box_text = (await self._inner_text(box)).strip()
                if not box_text:
                    return "unknown_reply_id"
                logger.warning(
                    "reply_submit_not_confirmed target=%s attempt=%s",
                    post_id,
                    attempt,
                )
            return None
        except Exception as exc:
            if self._is_driver_connection_closed(exc):
                raise RuntimeError("playwright_driver_connection_closed") from exc
            if self._is_target_closed_error(exc):
                raise RuntimeError("target_page_or_context_closed") from exc
            logger.warning("reply_action_failed target=%s error=%s", post_id, str(exc)[:260])
            return None

    async def comment_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_paths)

    async def reply_to_post(
        self,
        platform_post_id: str,
        text: str,
        image_paths: ImagePathInput | None = None,
    ) -> str | None:
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_paths)

    async def reply_with_image(self, platform_post_id: str, image_paths: ImagePathInput, text: str = "") -> str | None:
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
        if expected_kind == "post" and (bool(item.get("is_reply")) or bool(item.get("is_repost"))):
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
                if kind == "reply":
                    ok = await self._delete_owned_article(article, item, "reply", own_handle)
                elif kind == "repost":
                    ok = await self._undo_repost_article(article, post_id)
                else:
                    ok = await self._delete_owned_article(article, item, "post", own_handle)
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

    async def delete_all_posts(self) -> list[str]:
        return await self._delete_all_profile_items("post")

    async def delete_all_replies(self) -> list[str]:
        return await self._delete_all_profile_items("reply")

    async def delete_all_reposts(self) -> list[str]:
        return await self._delete_all_profile_items("repost")

    async def delete_all_content(self) -> dict[str, list[str]]:
        return {
            "reposts": await self.delete_all_reposts(),
            "replies": await self.delete_all_replies(),
            "posts": await self.delete_all_posts(),
        }

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
        metrics = self._zero_metrics()
        if not self.page:
            return metrics
        try:
            article = self.page.locator("article").first
            if await self._count_locator(article):
                article_metrics = await self._extract_article_metrics(article)
                for key in metrics:
                    metrics[key] = max(int(metrics.get(key) or 0), int(article_metrics.get(key) or 0))
            content = await self._page_content()
            content_metrics = self._extract_metrics_from_text(content)
            metrics["views"] = max(metrics["views"], int(content_metrics.get("views") or 0))
            metrics["likes"] = max(metrics["likes"], int(content_metrics.get("likes") or 0))
            metrics["replies"] = max(metrics["replies"], int(content_metrics.get("replies") or 0))
            metrics["comments"] = metrics["replies"]
            metrics["reposts"] = max(metrics["reposts"], int(content_metrics.get("reposts") or 0))
        except Exception as exc:
            logger.warning("post_metrics_parse_failed target=%s error=%s", platform_post_id, str(exc)[:260])
        return metrics

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
                    if self.STATUS_URL_RE.search(current_url):
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
                if self.STATUS_URL_RE.search(current_url):
                    await self.human.jitter(400, 1000)
                    return True
                content = await self._page_content()
                if f"/status/{post_id}" in content:
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

    async def _guess_recent_post_id(self) -> str | None:
        if not self.page:
            return None
        try:
            current = self.page.url
            match = self.STATUS_URL_RE.search(current or "")
            if match:
                return match.group(1)
            text = await self._page_content()
            found = self.STATUS_URL_RE.search(text)
            return found.group(1) if found else None
        except Exception:
            return None


XController = XTextAdapter
