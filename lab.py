from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapter import XController
from .settings import ControllerSettings


MUTATING_ACTIONS = {
    "delete_all_content",
    "delete_all_posts",
    "delete_all_replies",
    "delete_all_reposts",
    "delete_post",
    "delete_reply",
    "delete_repost",
    "like_post",
    "reply_to_post",
    "post_text",
    "follow_user",
    "unfollow_user",
    "like_post_and_measure",
    "reply_to_post_and_measure",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_lab_logger() -> logging.Logger:
    logger = logging.getLogger("x_controller.lab")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False

        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "x_controller_lab.log"
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

        file_handler = RotatingFileHandler(log_file, maxBytes=8_000_000, backupCount=4, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    # Route adapter-level diagnostics into the same lab log so UI/API troubleshooting is self-contained.
    adapter_logger = logging.getLogger("x_controller.adapter")
    adapter_logger.setLevel(logging.INFO)
    adapter_logger.propagate = False
    existing = {id(handler) for handler in adapter_logger.handlers}
    for handler in logger.handlers:
        if id(handler) not in existing:
            adapter_logger.addHandler(handler)
    return logger


@dataclass
class SessionState:
    profile_path: str = ""
    account_handle: str | None = None
    running: bool = False
    logged_in: bool | None = None
    updated_at: str = ""
    error: str | None = None


class ControllerLabManager:
    def __init__(self) -> None:
        self._adapter: XController | None = None
        self._settings = ControllerSettings()
        self._lock = asyncio.Lock()
        self._walkthrough_task: asyncio.Task | None = None
        self._session = SessionState(updated_at=_now_iso())
        self._walkthrough: dict[str, Any] = self._default_walkthrough_status()
        self._last_action_result: dict[str, Any] | None = None
        self._action_history: list[dict[str, Any]] = []
        self._logger = _setup_lab_logger()

    def _default_walkthrough_status(self) -> dict[str, Any]:
        return {
            "running": False,
            "completed": False,
            "current_action": "idle",
            "step_index": 0,
            "step_total": 0,
            "message": "not_started",
            "events": [],
            "updated_at": _now_iso(),
            "error": None,
            "eval": {},
        }

    def _event(self, action: str, message: str) -> None:
        events = self._walkthrough.get("events") if isinstance(self._walkthrough.get("events"), list) else []
        events.append({"at": _now_iso(), "action": action, "message": message})
        self._walkthrough["events"] = events[-40:]
        self._walkthrough["updated_at"] = _now_iso()
        self._logger.info("walkthrough_event action=%s message=%s", action, message[:240])

    def _set_walkthrough(self, **fields: Any) -> None:
        self._walkthrough.update(fields)
        self._walkthrough["updated_at"] = _now_iso()
        action = str(fields.get("current_action") or "")
        message = str(fields.get("message") or "")
        if action or message:
            self._event(action, message)

    def _require_adapter(self) -> XController:
        if not self._adapter:
            raise RuntimeError("session_not_started")
        return self._adapter

    def _is_closed_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        markers = (
            "target_page_or_context_closed",
            "target page, context or browser has been closed",
            "targetclosederror",
            "playwright_driver_connection_closed",
            "connection closed while reading from the driver",
            "connection closed while reading from driver",
        )
        return any(marker in text for marker in markers)

    async def _restart_adapter(self, reason: str) -> None:
        profile_path = (self._session.profile_path or "").strip()
        if not profile_path:
            raise RuntimeError("profile_path_required")
        self._logger.warning("adapter_restart reason=%s profile_path=%s", reason, profile_path)
        if self._adapter:
            with contextlib.suppress(Exception):
                await self._adapter.close()
        self._adapter = XController(profile_path, settings=self._settings)
        await self._adapter.start()
        self._session.running = True
        self._session.updated_at = _now_iso()

    def _record_action(
        self,
        action: str,
        status: str,
        duration_ms: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        row = {
            "at": _now_iso(),
            "action": action,
            "status": status,
            "duration_ms": int(duration_ms),
            "result": result or {},
            "error": error,
        }
        self._action_history.append(row)
        self._action_history = self._action_history[-80:]
        if status == "failed":
            self._logger.error("action_failed action=%s duration_ms=%s error=%s", action, duration_ms, (error or "")[:300])
        elif status == "retry":
            self._logger.warning("action_retry action=%s duration_ms=%s error=%s", action, duration_ms, (error or "")[:300])
        elif status == "no_effect":
            self._logger.warning("action_no_effect action=%s duration_ms=%s", action, duration_ms)
        else:
            self._logger.info("action_success action=%s duration_ms=%s", action, duration_ms)

    async def _run_action_with_recovery(
        self,
        action_name: str,
        run_once: Callable[[], Awaitable[dict[str, Any]]],
        allow_closed_retry: bool,
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await run_once()
                duration_ms = int((time.monotonic() - started) * 1000)
                outcome_ok = True
                actual_action = action_name
                expected_action = action_name
                if isinstance(result, dict):
                    if "ok" in result:
                        outcome_ok = bool(result.get("ok"))
                    actual_action = str(result.get("actual_action") or action_name)
                    expected_action = str(result.get("expected_action") or action_name)
                output = {
                    "action": action_name,
                    "attempt": attempt,
                    "result": result,
                    "ok": outcome_ok,
                    "expected_action": expected_action,
                    "actual_action": actual_action,
                    "duration_ms": duration_ms,
                    "at": _now_iso(),
                }
                self._last_action_result = output
                self._session.error = None
                self._session.updated_at = _now_iso()
                self._record_action(action_name, "success" if outcome_ok else "no_effect", duration_ms, result=result)
                return output
            except Exception as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                if allow_closed_retry and attempt == 1 and self._is_closed_error(exc):
                    self._record_action(action_name, "retry", duration_ms, error=str(exc))
                    await self._restart_adapter(f"{action_name}:closed_error")
                    continue
                self._session.error = str(exc)[:320]
                self._session.updated_at = _now_iso()
                self._record_action(action_name, "failed", duration_ms, error=str(exc))
                raise

    async def _cancel_walkthrough_task(self) -> None:
        task = self._walkthrough_task
        self._walkthrough_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _serialize_post(self, item: Any) -> dict[str, Any]:
        raw_payload = getattr(item, "raw", {})
        raw = raw_payload if isinstance(raw_payload, dict) else {}
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else raw
        views = int(metrics.get("views") or 0) if isinstance(metrics, dict) else 0
        likes = int(metrics.get("likes") or 0) if isinstance(metrics, dict) else 0
        replies = int(metrics.get("replies") or metrics.get("comments") or 0) if isinstance(metrics, dict) else 0
        reposts = int(metrics.get("reposts") or 0) if isinstance(metrics, dict) else 0
        return {
            "platform_post_id": str(getattr(item, "platform_post_id", "") or ""),
            "author": str(getattr(item, "author", "") or ""),
            "text": str(getattr(item, "text", "") or "")[:500],
            "views": views,
            "likes": likes,
            "replies": replies,
            "comments": replies,
            "reposts": reposts,
            "raw": raw,
        }

    async def _pick_random_visible_post(self, adapter: XController, sample_limit: int = 20) -> dict[str, Any]:
        limit = max(3, min(int(sample_limit), 60))
        posts = await adapter.read_visible_posts(limit=limit)
        if not posts:
            raise RuntimeError("no_posts_found")
        pick = random.choice(posts[: min(len(posts), limit)])
        serialized = self._serialize_post(pick)
        post_id = str(serialized.get("platform_post_id") or "").strip()
        if not post_id:
            raise RuntimeError("no_posts_found")
        return {"post_id": post_id, "post": serialized}

    async def start_session(self, profile_path: str, account_handle: str | None = None) -> dict[str, Any]:
        path = str(profile_path or "").strip()
        if not path:
            raise RuntimeError("profile_path_required")
        async with self._lock:
            await self._cancel_walkthrough_task()
            if self._adapter:
                with contextlib.suppress(Exception):
                    await self._adapter.close()
            self._adapter = XController(path, settings=self._settings)
            await self._adapter.start()
            self._session = SessionState(
                profile_path=path,
                account_handle=(account_handle or "").strip() or None,
                running=True,
                logged_in=None,
                updated_at=_now_iso(),
                error=None,
            )
            self._walkthrough = self._default_walkthrough_status()
            self._action_history = []
            self._logger.info("session_started profile_path=%s account_handle=%s", path, self._session.account_handle or "-")
        return self.session_status()

    async def stop_session(self) -> dict[str, Any]:
        async with self._lock:
            await self._cancel_walkthrough_task()
            if self._adapter:
                with contextlib.suppress(Exception):
                    await self._adapter.close()
            self._adapter = None
            self._session.running = False
            self._session.logged_in = None
            self._session.updated_at = _now_iso()
            self._logger.info("session_stopped")
        return self.session_status()

    async def open_login(self) -> dict[str, Any]:
        adapter = self._require_adapter()
        await adapter.open_login_page()
        self._session.updated_at = _now_iso()
        return self.session_status()

    async def refresh_login_status(self) -> dict[str, Any]:
        adapter = self._require_adapter()
        try:
            logged = await adapter.is_logged_in()
            self._session.logged_in = bool(logged)
            self._session.error = None
        except Exception as exc:
            self._session.logged_in = None
            self._session.error = str(exc)[:220]
        self._session.updated_at = _now_iso()
        return self.session_status()

    async def run_action(self, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            if self._walkthrough_task and not self._walkthrough_task.done():
                raise RuntimeError("walkthrough_running")

            payload = dict(args or {})
            name = str(action or "").strip().lower()
            if not name:
                raise RuntimeError("action_required")

            async def _run_once() -> dict[str, Any]:
                adapter = self._require_adapter()
                def _action_payload(expected_action: str, actual_action: str, ok: bool = True, **extra: Any) -> dict[str, Any]:
                    row = {
                        "ok": bool(ok),
                        "expected_action": expected_action,
                        "actual_action": actual_action,
                    }
                    row.update(extra)
                    return row

                async def _resolve_post_target() -> tuple[str, dict[str, Any], str]:
                    post_id = str(payload.get("post_id") or "").strip()
                    selected: dict[str, Any] = {}
                    target_mode = str(payload.get("target_mode") or "").strip().lower()
                    if not post_id and target_mode in {"random_visible", "random_timeline", "random"}:
                        picked = await self._pick_random_visible_post(adapter, sample_limit=int(payload.get("sample_limit") or 20))
                        post_id = str(picked.get("post_id") or "").strip()
                        selected = picked.get("post") if isinstance(picked.get("post"), dict) else {}
                    if not post_id:
                        raise RuntimeError("post_id_required")
                    return post_id, selected, (target_mode or "post_id")

                if name == "read_timeline":
                    limit = max(1, min(int(payload.get("limit", 20)), 120))
                    posts = await adapter.read_timeline(limit=limit)
                    return _action_payload(
                        "read_timeline",
                        "read_timeline",
                        ok=True,
                        count=len(posts),
                        posts=[self._serialize_post(row) for row in posts[:25]],
                    )
                if name == "read_visible_posts":
                    limit = max(1, min(int(payload.get("limit", 20)), 120))
                    posts = await adapter.read_visible_posts(limit=limit)
                    return _action_payload(
                        "read_visible_posts",
                        "read_visible_posts",
                        ok=True,
                        count=len(posts),
                        posts=[self._serialize_post(row) for row in posts[:25]],
                    )
                if name == "search_posts":
                    query = str(payload.get("query") or "").strip()
                    if not query:
                        raise RuntimeError("query_required")
                    limit = max(1, min(int(payload.get("limit", 20)), 120))
                    posts = await adapter.search_posts(query, limit=limit)
                    return _action_payload(
                        "search_posts",
                        "search_posts",
                        ok=True,
                        count=len(posts),
                        posts=[self._serialize_post(row) for row in posts[:25]],
                        query=query,
                    )
                if name == "view_post":
                    post_id, selected, target_mode = await _resolve_post_target()
                    ok = bool(await adapter.view_post(post_id, dwell_seconds=(4, 9)))
                    return _action_payload(
                        "view_post",
                        "view_post" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "like_post":
                    post_id, selected, target_mode = await _resolve_post_target()
                    ok = bool(await adapter.like_post(post_id))
                    return _action_payload(
                        "like_post",
                        "like_post" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "reply_to_post":
                    post_id, selected, target_mode = await _resolve_post_target()
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        raise RuntimeError("text_required")
                    reply_id = await adapter.reply_to_post(post_id, text)
                    ok = bool(reply_id) and str(reply_id) != "unknown_reply_id"
                    return _action_payload(
                        "reply_to_post",
                        "reply_to_post" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        reply_id=reply_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "post_text":
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        raise RuntimeError("text_required")
                    post_id = await adapter.post_text(text)
                    ok = bool(post_id)
                    return _action_payload(
                        "post_text",
                        "post_text" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                    )
                if name == "like_post_and_measure":
                    post_id, selected, target_mode = await _resolve_post_target()
                    like_ok = bool(await adapter.like_post(post_id))
                    metrics = await adapter.post_metrics(post_id) if like_ok else {}
                    return _action_payload(
                        "like_post_and_measure",
                        "like_post+post_metrics" if like_ok else "none",
                        ok=like_ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                        like_ok=like_ok,
                        metrics=metrics,
                    )
                if name == "reply_to_post_and_measure":
                    post_id, selected, target_mode = await _resolve_post_target()
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        raise RuntimeError("text_required")
                    reply_id = await adapter.reply_to_post(post_id, text)
                    reply_ok = bool(reply_id) and str(reply_id) != "unknown_reply_id"
                    metrics = await adapter.post_metrics(post_id) if reply_ok else {}
                    return _action_payload(
                        "reply_to_post_and_measure",
                        "reply_to_post+post_metrics" if reply_ok else "none",
                        ok=reply_ok,
                        post_id=post_id,
                        reply_id=reply_id,
                        target_mode=target_mode,
                        selected_post=selected,
                        metrics=metrics,
                    )
                if name == "profile_recent_metrics":
                    handle = str(payload.get("username") or self._session.account_handle or "").strip()
                    if not handle:
                        raise RuntimeError("username_required")
                    limit = max(1, min(int(payload.get("limit", 12)), 80))
                    rows = await adapter.profile_recent_metrics(handle, limit=limit)
                    return _action_payload(
                        "profile_recent_metrics",
                        "profile_recent_metrics",
                        ok=True,
                        username=handle,
                        count=len(rows),
                        rows=rows[:30],
                    )
                if name == "post_metrics":
                    post_id, selected, target_mode = await _resolve_post_target()
                    metrics = await adapter.post_metrics(post_id)
                    return _action_payload(
                        "post_metrics",
                        "post_metrics",
                        ok=True,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                        metrics=metrics,
                    )
                if name == "delete_post":
                    post_id, selected, target_mode = await _resolve_post_target()
                    ok = bool(await adapter.delete_post(post_id))
                    return _action_payload(
                        "delete_post",
                        "delete_post" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "delete_reply":
                    post_id, selected, target_mode = await _resolve_post_target()
                    ok = bool(await adapter.delete_reply(post_id))
                    return _action_payload(
                        "delete_reply",
                        "delete_reply" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "delete_repost":
                    post_id, selected, target_mode = await _resolve_post_target()
                    ok = bool(await adapter.delete_repost(post_id))
                    return _action_payload(
                        "delete_repost",
                        "delete_repost" if ok else "none",
                        ok=ok,
                        post_id=post_id,
                        target_mode=target_mode,
                        selected_post=selected,
                    )
                if name == "delete_all_posts":
                    deleted = await adapter.delete_all_posts()
                    return _action_payload(
                        "delete_all_posts",
                        "delete_all_posts" if deleted else "none",
                        ok=bool(deleted),
                        count=len(deleted),
                        urls=deleted,
                    )
                if name == "delete_all_replies":
                    deleted = await adapter.delete_all_replies()
                    return _action_payload(
                        "delete_all_replies",
                        "delete_all_replies" if deleted else "none",
                        ok=bool(deleted),
                        count=len(deleted),
                        urls=deleted,
                    )
                if name == "delete_all_reposts":
                    deleted = await adapter.delete_all_reposts()
                    return _action_payload(
                        "delete_all_reposts",
                        "delete_all_reposts" if deleted else "none",
                        ok=bool(deleted),
                        count=len(deleted),
                        urls=deleted,
                    )
                if name == "delete_all_content":
                    deleted = await adapter.delete_all_content()
                    total_count = sum(len(rows) for rows in deleted.values())
                    return _action_payload(
                        "delete_all_content",
                        "delete_all_content" if total_count > 0 else "none",
                        ok=total_count > 0,
                        count=total_count,
                        result_by_kind=deleted,
                    )
                if name == "follow_user":
                    username = str(payload.get("username") or "").strip()
                    if not username:
                        raise RuntimeError("username_required")
                    ok = bool(await adapter.follow_user(username))
                    return _action_payload(
                        "follow_user",
                        "follow_user" if ok else "none",
                        ok=ok,
                        username=username,
                    )
                if name == "unfollow_user":
                    username = str(payload.get("username") or "").strip()
                    if not username:
                        raise RuntimeError("username_required")
                    ok = bool(await adapter.unfollow_user(username))
                    return _action_payload(
                        "unfollow_user",
                        "unfollow_user" if ok else "none",
                        ok=ok,
                        username=username,
                    )
                if name == "recover_home":
                    ok = bool(await adapter.recover_home(force_nav=bool(payload.get("force_nav", False))))
                    return _action_payload(
                        "recover_home",
                        "recover_home" if ok else "none",
                        ok=ok,
                    )
                if name == "is_logged_in":
                    ok = bool(await adapter.is_logged_in())
                    self._session.logged_in = bool(ok)
                    return _action_payload(
                        "is_logged_in",
                        "is_logged_in",
                        ok=ok,
                    )
                raise RuntimeError(f"unknown_action:{name}")

            return await self._run_action_with_recovery(
                action_name=name,
                run_once=_run_once,
                allow_closed_retry=name not in MUTATING_ACTIONS,
            )

    async def start_walkthrough(
        self,
        search_query: str = "unpopular opinion",
        timeline_limit: int = 12,
        cooldown_min_seconds: int = 8,
        cooldown_max_seconds: int = 18,
        reply_text: str = "Walkthrough reply check.",
        post_text: str = "",
        include_eval: bool = True,
        eval_limit: int = 12,
    ) -> dict[str, Any]:
        async with self._lock:
            self._require_adapter()
            if self._walkthrough_task and not self._walkthrough_task.done():
                return self.walkthrough_status()

            query = (search_query or "").strip() or "unpopular opinion"
            reply = (reply_text or "").strip() or "Walkthrough reply check."
            post_body = (post_text or "").strip()
            if not post_body:
                post_body = f"Walkthrough post check {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
            t_limit = max(6, min(int(timeline_limit), 40))
            cd_min = max(1, min(int(cooldown_min_seconds), 90))
            cd_max = max(cd_min, min(int(cooldown_max_seconds), 180))
            e_limit = max(3, min(int(eval_limit), 40))

            self._set_walkthrough(
                running=True,
                completed=False,
                current_action="starting",
                step_index=0,
                step_total=8 if include_eval else 7,
                message="starting_walkthrough",
                error=None,
                eval={},
            )
            self._logger.info(
                "walkthrough_started query=%s timeline_limit=%s cooldown=%s..%s include_eval=%s",
                query,
                t_limit,
                cd_min,
                cd_max,
                include_eval,
            )

            async def _run() -> None:
                step = 0
                processed_posts: dict[str, dict[str, Any]] = {}

                async def step_status(action: str, message: str) -> None:
                    nonlocal step
                    step += 1
                    self._set_walkthrough(
                        running=True,
                        completed=False,
                        current_action=action,
                        step_index=step,
                        message=message,
                        error=None,
                    )

                async def cooldown() -> None:
                    duration = random.uniform(cd_min, cd_max)
                    self._event("cooldown", f"sleeping_{duration:.1f}s")
                    await asyncio.sleep(duration)

                def track_post(item: Any) -> None:
                    serialized = self._serialize_post(item)
                    post_id = str(serialized.get("platform_post_id") or "").strip()
                    if not post_id:
                        return
                    processed_posts[post_id] = serialized

                try:
                    await step_status("auth_check", "checking_login_state")
                    if not await self._require_adapter().is_logged_in():
                        await self._require_adapter().open_login_page()
                        raise RuntimeError("login_required")
                    self._session.logged_in = True

                    await step_status("timeline_read", "reading_timeline")
                    observed = await self._require_adapter().read_timeline(limit=t_limit)
                    if not observed:
                        await step_status("timeline_search", "timeline_empty_searching")
                        observed = await self._require_adapter().search_posts(query, limit=t_limit)
                    if not observed:
                        raise RuntimeError("no_posts_found")
                    for row in observed[: min(len(observed), max(10, t_limit))]:
                        track_post(row)
                    first = observed[0]
                    second = observed[1] if len(observed) > 1 else observed[0]

                    await step_status("view", f"viewing_post_{first.platform_post_id}")
                    if not await self._require_adapter().view_post(first.platform_post_id, dwell_seconds=(6, 14)):
                        raise RuntimeError("view_failed")
                    await cooldown()

                    await step_status("like", f"liking_post_{first.platform_post_id}")
                    if not await self._require_adapter().like_post(first.platform_post_id):
                        raise RuntimeError("like_failed")
                    await cooldown()

                    await step_status("reply", f"replying_post_{second.platform_post_id}")
                    reply_id = await self._require_adapter().reply_to_post(second.platform_post_id, reply)
                    if not reply_id:
                        raise RuntimeError("reply_failed")
                    await cooldown()

                    await step_status("search", f"searching_{query}")
                    searched = await self._require_adapter().search_posts(query, limit=max(8, t_limit // 2))
                    for row in searched[: min(len(searched), max(8, t_limit // 2))]:
                        track_post(row)
                    await cooldown()

                    await step_status("post", "posting_walkthrough_text")
                    posted_id = await self._require_adapter().post_text(post_body)
                    if not posted_id:
                        raise RuntimeError("post_failed")
                    await cooldown()

                    eval_payload: dict[str, Any] = {}
                    if include_eval:
                        await step_status("doing_eval", "collecting_profile_and_post_metrics")
                        post_metrics = await self._require_adapter().post_metrics(posted_id)
                        handle = (self._session.account_handle or "").strip()
                        if handle:
                            profile_rows = await self._require_adapter().profile_recent_metrics(handle, limit=e_limit)
                        else:
                            profile_rows = []

                        processed_eval_rows: list[dict[str, Any]] = []
                        metric_probe_budget = min(10, max(4, e_limit))
                        for idx, (pid, row) in enumerate(processed_posts.items()):
                            if idx >= metric_probe_budget:
                                break
                            merged = dict(row)
                            with contextlib.suppress(Exception):
                                pm = await self._require_adapter().post_metrics(pid)
                                if isinstance(pm, dict):
                                    merged["views"] = int(pm.get("views") or merged.get("views") or 0)
                                    merged["likes"] = int(pm.get("likes") or merged.get("likes") or 0)
                                    merged["replies"] = int(pm.get("replies") or merged.get("replies") or merged.get("comments") or 0)
                                    merged["comments"] = int(pm.get("comments") or merged.get("comments") or merged.get("replies") or 0)
                                    merged["reposts"] = int(pm.get("reposts") or merged.get("reposts") or 0)
                            processed_eval_rows.append(merged)

                        eval_payload = {
                            "post_id": posted_id,
                            "post_metrics": post_metrics,
                            "profile_handle": handle or None,
                            "profile_recent_metrics": profile_rows,
                            "processed_posts": list(processed_posts.values())[:80],
                            "processed_posts_with_metrics": processed_eval_rows,
                            "posts": [
                                {
                                    "post_id": posted_id,
                                    "views": int(post_metrics.get("views") or 0),
                                    "likes": int(post_metrics.get("likes") or 0),
                                    "replies": int(post_metrics.get("replies") or 0),
                                    "reposts": int(post_metrics.get("reposts") or 0),
                                    "source": "walkthrough_post_metrics",
                                },
                                *[
                                    {
                                        "post_id": str(row.get("post_id") or ""),
                                        "views": int(row.get("views") or 0),
                                        "likes": int(row.get("likes") or 0),
                                        "replies": int(row.get("replies") or row.get("comments") or 0),
                                        "reposts": int(row.get("reposts") or 0),
                                        "source": str(row.get("source") or "profile_scan"),
                                    }
                                    for row in profile_rows
                                    if isinstance(row, dict)
                                ],
                            ][:60],
                        }
                        self._walkthrough["eval"] = eval_payload

                    self._set_walkthrough(
                        running=False,
                        completed=True,
                        current_action="completed",
                        message="walkthrough_completed",
                        error=None,
                        eval=eval_payload if include_eval else {},
                    )
                    self._logger.info("walkthrough_completed")
                except asyncio.CancelledError:
                    self._set_walkthrough(
                        running=False,
                        completed=False,
                        current_action="cancelled",
                        message="walkthrough_cancelled",
                        error=None,
                    )
                    self._logger.info("walkthrough_cancelled")
                    raise
                except Exception as exc:
                    self._set_walkthrough(
                        running=False,
                        completed=False,
                        current_action="failed",
                        message="walkthrough_failed",
                        error=str(exc)[:320],
                    )
                    self._logger.error("walkthrough_failed error=%s", str(exc)[:320])

            self._walkthrough_task = asyncio.create_task(_run())
            return self.walkthrough_status()

    async def stop_walkthrough(self) -> dict[str, Any]:
        await self._cancel_walkthrough_task()
        self._set_walkthrough(
            running=False,
            completed=False,
            current_action="stopped",
            message="walkthrough_stopped",
        )
        self._logger.info("walkthrough_stopped")
        return self.walkthrough_status()

    def session_status(self) -> dict[str, Any]:
        return {
            "profile_path": self._session.profile_path,
            "account_handle": self._session.account_handle,
            "running": self._session.running,
            "logged_in": self._session.logged_in,
            "updated_at": self._session.updated_at or _now_iso(),
            "error": self._session.error,
            "last_action_result": self._last_action_result,
            "walkthrough_running": bool(self._walkthrough_task and not self._walkthrough_task.done()),
            "action_history": list(self._action_history[-40:]),
        }

    def walkthrough_status(self) -> dict[str, Any]:
        return dict(self._walkthrough)
