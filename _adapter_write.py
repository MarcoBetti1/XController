"""Posting, engagement, and content-mutation flows for XTextAdapter."""

from __future__ import annotations

import contextlib
import logging
import random
import re
import time
import warnings
from typing import Any

from . import _ui_selectors as ui
from ._adapter_runtime import ImagePathInput
from .base import ActionResult

logger = logging.getLogger(__name__)


class _AdapterWriteMixin:
    """State-mutating write and cleanup flows for X posts, replies, quotes, and follows."""
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
        """Create a post and return structured action diagnostics.

        Args:
            text: Post body text to submit.
            image_paths: Optional image path(s) to attach before submit.
        """
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
        """Create a post and return the created post id when available.

        Args:
            text: Post body text to submit.
            image_paths: Optional image path(s) to attach before submit.
        """
        result = await self.post_text_detailed(text, image_paths=image_paths)
        return result.created_post_id if result.ok else None

    async def post_image(self, image_paths: ImagePathInput, text: str = "") -> str | None:
        """Create a post with one or more images (deprecated compatibility helper).

        Args:
            image_paths: Image path(s) to attach.
            text: Optional text content to include with the media post.
        """
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
        """Run the `engage_post` interaction workflow against a target post."""
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
        """Run the `like_post_detailed` interaction workflow against a target post."""
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
        """Run the `like_post` interaction workflow against a target post."""
        result = await self.like_post_detailed(platform_post_id)
        return result.ok

    async def view_post(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool:
        """Run the `view_post` interaction workflow against a target post."""
        result = await self.engage_post(platform_post_id, do_view=True, do_like=False, dwell_seconds=dwell_seconds)
        return bool(result.get("viewed"))

    async def view_post_detailed(self, platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> ActionResult:
        """Run the `view_post_detailed` interaction workflow against a target post."""
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
        """Run the `quote_post` interaction workflow against a target post."""
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
        """Run the `quote_post_detailed` interaction workflow against a target post."""
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
        """Run the `quote_post_with_image` interaction workflow against a target post."""
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
        """Run the `reply_to_post_detailed` interaction workflow against a target post."""
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
        """Run the `reply_to_post` interaction workflow against a target post."""
        return await self._reply_to_post_impl(platform_post_id, text, image_paths=image_paths)

    async def reply_with_image(self, platform_post_id: str, image_paths: ImagePathInput, text: str = "") -> str | None:
        """Run the `reply_with_image` interaction workflow against a target post."""
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
        """Delete owned X content using the `delete_post` flow."""
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
        """Delete owned X content using the `delete_reply` flow."""
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
        """Delete owned X content using the `delete_repost` flow."""
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
        """Delete owned X content using the `delete_quote` flow."""
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
        """Delete owned X content using the `delete_all_posts` flow."""
        return await self._delete_all_profile_items("post")

    async def delete_all_replies(self) -> list[str]:
        """Delete owned X content using the `delete_all_replies` flow."""
        return await self._delete_all_profile_items("reply")

    async def delete_all_reposts(self) -> list[str]:
        """Delete owned X content using the `delete_all_reposts` flow."""
        return await self._delete_all_profile_items("repost")

    async def delete_all_quotes(self) -> list[str]:
        """Delete owned X content using the `delete_all_quotes` flow."""
        return await self._delete_all_profile_items("quote")

    async def delete_all_content(self) -> dict[str, list[str]]:
        """Delete owned X content using the `delete_all_content` flow."""
        return {
            "reposts": await self.delete_all_reposts(),
            "quotes": await self.delete_all_quotes(),
            "replies": await self.delete_all_replies(),
            "posts": await self.delete_all_posts(),
        }

    async def delete_post_detailed(self, platform_post_id: str, kind: str = "post") -> ActionResult:
        """Delete owned X content using the `delete_post_detailed` flow."""
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
        """Execute the `repost_post_detailed` controller operation."""
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
        """Run the `follow_user_detailed` follow-state action on the target account."""
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
        """Run the `unfollow_user_detailed` follow-state action on the target account."""
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
        """Run the `follow_user` follow-state action on the target account."""
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
        """Run the `unfollow_user` follow-state action on the target account."""
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
        """Create content on X using the `post_metrics` flow."""
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
        logger.warning("post_metrics_target_article_missing target=%s rendered_articles=%s", post_id, article_count)
        return None

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
