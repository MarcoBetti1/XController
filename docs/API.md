# API Notes

This document describes the public package surface shipped on `main`.

This package exposes one main controller class:

- `XController`
- `XTextAdapter` (compatibility alias of `XController`)

Additional exported diagnostics types:

- `AccountStats`
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

- `await return_home(force_refresh: bool = False) -> bool`
  Returns to home if possible. When `force_refresh=True`, the controller reloads the home surface after returning.

## Read Operations

- `await read_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await read_timeline_detailed(limit: int = 20, tab: str = "for_you", force_refresh: bool = False, reset_scroll: bool = False) -> TimelineReadResult`
- `await read_following_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await search_posts(query: str, limit: int = 10) -> list[ObservedPostData]`
- `await read_visible_posts(limit: int = 20) -> list[ObservedPostData]`
- `await read_notifications(limit: int = 20, unread_only: bool = False) -> list[ObservedNotificationData]`
- `await read_mentions(account_handle: str, hours_back: int = 2, limit: int = 120, ...)`
- `await read_post_thread_context(post_id, limit: int = 6, ...) -> list[ObservedPostData]`
- `await account_stats(handle: str | None = None) -> AccountStats`
- `await profile_recent_metrics(username: str, limit: int = 40) -> list[dict[str, int | str]]`
- `await post_metrics(platform_post_id: str) -> dict[str, int]`

`read_notifications(unread_only=True)` returns unread notifications without a separate alias method.

`read_timeline_detailed(force_refresh=True)` performs a home reload before collecting timeline posts. `reset_scroll=True` only presses Home to read the newest visible DOM items at the top of the feed; it does not reload the UI.

`account_stats()` samples public profile-level data: handle, display name, profile URL, followers, following, posts, likes, media, verified state, bio, location, and joined date text. When `handle` is omitted, it uses the authenticated account when detectable and falls back to the current profile surface if needed. Counts are normalized from compact X strings such as `1.2K`, `3.4M`, and `5B`. Unavailable fields remain zero or `None`, and `raw["warnings"]` plus `raw["current_url"]` explain what was missing. Browser transport failures such as `profile_in_use`, `playwright_driver_connection_closed`, and `target_page_or_context_closed` are raised as `RuntimeError`.

## Write / Engagement Operations

- `await post_text(text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await view_post(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool`
- `await like_post(platform_post_id: str) -> bool`
- `await reply_to_post(platform_post_id: str, text: str, image_paths: str | Sequence[str] | None = None) -> str | None`
- `await quote_post(platform_post_id: str, text: str = "", image_paths: str | Sequence[str] | None = None) -> str | None`
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
- `comment_post()` has been removed. Use `reply_to_post()`.
- `read_unread_notifications()` has been removed. Use `read_notifications(unread_only=True)`.
- `recover_home()` and `refresh_home()` have been replaced by `return_home(force_refresh=False)`.
- `post_text()`, `reply_to_post()`, and `quote_post()` accept `image_paths` for local image uploads. `post_image()`, `reply_with_image()`, and `quote_post_with_image()` remain only as deprecated compatibility wrappers.
- Delete methods verify ownership before deleting authored content.
  `delete_repost()` verifies repost state instead of authorship because reposts target another author's post.
- Bulk delete methods run until the relevant profile surface is exhausted; they no longer expose a caller-supplied item limit.

## Diagnostics

`XTextAdapter.last_action_error` holds the latest soft UI failure that was converted into a boolean/empty-result outcome.

For long-running services, prefer detailed methods over legacy compact methods. The compact methods are kept for compatibility and return the same shapes as before; a future major version is expected to make the `ActionResult`-returning methods the default.

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

`AccountStats` contains:

- `handle`
- `display_name`
- `profile_url`
- `followers`
- `following`
- `posts`
- `likes`
- `media`
- `verified`
- `bio`
- `location`
- `joined_at`
- `captured_at`
- `raw`

`raw` includes `current_url`, `target_url`, `warnings`, and parser diagnostics such as count sources.

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
