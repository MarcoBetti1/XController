# API Notes

This document describes the public package surface shipped on `main`.

This package exposes one main controller class:

- `XController`
- `XTextAdapter` (compatibility alias of `XController`)

Additional exported diagnostics types:

- `ActionFailureInfo`
- `ActionPreflight`
- `ActionResult`
- `ControllerHealth`
- `MediaPreflight`
- `ObservedMediaData`
- `TimelineReadResult`
- `UIActionError`

## Session Lifecycle

- `await start()`: starts a persistent Chromium context
- `await close()`: closes the browser context and Playwright resources
- `await is_logged_in() -> bool`
- `await open_login_page() -> None`
- `await current_state() -> dict[str, str]`
  When a soft UI failure was recorded, `current_state()` also includes `last_action_error`.

## Navigation / Recovery

- `await recover_home(force_nav: bool = False) -> bool`
  Returns to home if possible without forcing a reload.
- `await refresh_home(force_nav: bool = False) -> bool`
  Returns home and attempts a reload.

## Read Operations

- `await read_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await read_timeline_detailed(limit: int = 20, tab: str = "for_you", force_refresh: bool = False, reset_scroll: bool = False) -> TimelineReadResult`
- `await read_following_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await search_posts(query: str, limit: int = 10) -> list[ObservedPostData]`
- `await read_visible_posts(limit: int = 20) -> list[ObservedPostData]`
- `await read_notifications(limit: int = 20, unread_only: bool = False) -> list[ObservedNotificationData]`
- `await read_unread_notifications(limit: int = 20) -> list[ObservedNotificationData]`
- `await read_mentions(account_handle: str, hours_back: int = 2, limit: int = 120, ...)`
- `await read_post_thread_context(post_id, limit: int = 6, ...) -> list[ObservedPostData]`
- `await profile_recent_metrics(username: str, limit: int = 40) -> list[dict[str, int | str]]`
- `await post_metrics(platform_post_id: str) -> dict[str, int]`

## Write / Engagement Operations

- `await post_text(text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await post_image(image_paths: str | Sequence[str], text: str = "") -> str | None`
- `await view_post(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool`
- `await like_post(platform_post_id: str) -> bool`
- `await reply_to_post(platform_post_id: str, text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await reply_with_image(platform_post_id: str, image_paths: str | Sequence[str], text: str = "") -> str | None`
- `await comment_post(platform_post_id: str, text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await quote_post(platform_post_id: str, text: str = "", image_paths: str | Sequence[str] | None = None) -> str | None`
- `await quote_post_with_image(platform_post_id: str, image_paths: str | Sequence[str], text: str = "") -> str | None`
- `await delete_post(platform_post_id: str) -> bool`
- `await delete_reply(platform_post_id: str) -> bool`
- `await delete_repost(platform_post_id: str) -> bool`
- `await delete_all_posts() -> list[str]`
- `await delete_all_replies() -> list[str]`
- `await delete_all_reposts() -> list[str]`
- `await delete_all_content() -> dict[str, list[str]]`
- `await follow_user(username: str) -> bool`
- `await unfollow_user(username: str) -> bool`
- `await engage_post(platform_post_id: str, do_view: bool = True, do_like: bool = False, ...) -> dict[str, bool]`

Detailed write/action variants:

- `await post_text_detailed(text: str, image_paths: ...) -> ActionResult`
- `await reply_to_post_detailed(platform_post_id: str, text: str, image_paths: ...) -> ActionResult`
- `await quote_post_detailed(platform_post_id: str, text: str = "", image_paths: ...) -> ActionResult`
- `await like_post_detailed(platform_post_id: str) -> ActionResult`
- `await view_post_detailed(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> ActionResult`
- `await repost_post_detailed(platform_post_id: str) -> ActionResult`
- `await follow_user_detailed(username: str) -> ActionResult`
- `await unfollow_user_detailed(username: str) -> ActionResult`
- `await delete_post_detailed(platform_post_id: str, kind: str = "post") -> ActionResult`

Preflight and diagnostics:

- `await preflight_action(platform_post_id: str, action: str = "reply", open_composer: bool = False) -> ActionPreflight`
- `await attach_images_preflight(image_paths) -> MediaPreflight`
- `await current_surface() -> dict[str, str]`
- `await settle_home(tab: str = "for_you", force_nav: bool = False) -> bool`
- `await health_check() -> ControllerHealth`
- `await debug_snapshot(output_dir, article_limit: int = 12) -> dict`

## Compatibility Guidance

- `reply_to_post()` is the canonical X-specific method name.
- `comment_post()` is kept for backward compatibility and delegates to the same reply implementation.
- `post_text()`, `reply_to_post()`, and `quote_post()` accept `image_paths` for local image uploads.
  Convenience wrappers are available when the image is the primary payload.
- `recover_home()` and `refresh_home()` are intentionally both kept.
  `recover_home()` is lighter-weight.
  `refresh_home()` includes a reload attempt.
- Delete methods verify ownership before deleting authored content.
  `delete_repost()` verifies repost state instead of authorship because reposts target another author's post.
- Bulk delete methods run until the relevant profile surface is exhausted; they no longer expose a caller-supplied item limit.

## Diagnostics

`XTextAdapter.last_action_error` holds the latest soft UI failure that was converted into a boolean/empty-result outcome.

For long-running services, prefer detailed methods over legacy compact methods. The compact methods are kept for compatibility and return the same shapes as before.

`ActionFailureInfo` contains:

- `action`
- `error_type`
- `message`
- `url`
- `selector`
- `occurred_at`

Set `ControllerSettings(strict_ui_failures=True)` to turn those soft UI failures into `UIActionError`.

## Data Model

`ObservedPostData` contains:

- `platform_post_id`
- `author`
- `text`
- `raw`

Convenience helpers:

- `.metrics`: returns `raw["metrics"]` when present
- `.author_limited`: true when X shows an author-controlled post limit, such as restricted replies
- `.reply_limited`: true when the detected author limit affects replies
- `.author_limit_notice`: the detected X notice text, for example `Only some accounts can reply.`
- `.to_dict()`: returns a serializable copy

`ObservedNotificationData` contains:

- `notification_id`
- `notification_type`
- `actor`
- `text`
- `raw`

Convenience helpers:

- `.platform_post_id`: returns the linked post id when present
- `.unread`: returns the best-effort unread flag
- `.author_limited`: true when X shows an author-controlled post limit on the linked post
- `.reply_limited`: true when the detected author limit affects replies
- `.author_limit_notice`: the detected X notice text
- `.to_dict()`: returns a serializable copy

## Settings

`ControllerSettings` accepts:

- another `ControllerSettings`
- a plain object with matching attributes
- a mapping/dict with matching keys

Use `ControllerSettings.to_dict()` when you need to persist or inspect the active values.

Notable maintainability setting:

- `strict_ui_failures`
  Raises `UIActionError` for soft UI failures instead of quietly returning fallback values.
- `playwright_mode`
  Accepts `"auto"`, `"async"`, or `"sync"` to control whether startup prefers async Playwright or the sync fallback.
- `prefer_sync_playwright`
  Optional boolean override for callers that need explicit runtime/thread ownership.
