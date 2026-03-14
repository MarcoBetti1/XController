# API Notes

This package exposes one main controller class:

- `XController`
- `XTextAdapter` (compatibility alias of `XController`)

## Session Lifecycle

- `await start()`: starts a persistent Chromium context
- `await close()`: closes the browser context and Playwright resources
- `await is_logged_in() -> bool`
- `await open_login_page() -> None`
- `await current_state() -> dict[str, str]`

## Navigation / Recovery

- `await recover_home(force_nav: bool = False) -> bool`
  Returns to home if possible without forcing a reload.
- `await refresh_home(force_nav: bool = False) -> bool`
  Returns home and attempts a reload.

## Read Operations

- `await read_timeline(limit: int = 20) -> list[ObservedPostData]`
- `await search_posts(query: str, limit: int = 10) -> list[ObservedPostData]`
- `await read_visible_posts(limit: int = 20) -> list[ObservedPostData]`
- `await read_mentions(account_handle: str, hours_back: int = 2, limit: int = 120, ...)`
- `await profile_recent_metrics(username: str, limit: int = 40) -> list[dict[str, int | str]]`
- `await post_metrics(platform_post_id: str) -> dict[str, int]`

## Write / Engagement Operations

- `await post_text(text: str) -> str | None`
- `await view_post(platform_post_id: str, dwell_seconds: tuple[int, int] = (3, 8)) -> bool`
- `await like_post(platform_post_id: str) -> bool`
- `await reply_to_post(platform_post_id: str, text: str) -> str | None`
- `await comment_post(platform_post_id: str, text: str) -> str | None`
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

## Compatibility Guidance

- `reply_to_post()` is the canonical X-specific method name.
- `comment_post()` is kept for backward compatibility and delegates to the same reply implementation.
- `recover_home()` and `refresh_home()` are intentionally both kept.
  `recover_home()` is lighter-weight.
  `refresh_home()` includes a reload attempt.
- Delete methods verify ownership before deleting authored content.
  `delete_repost()` verifies repost state instead of authorship because reposts target another author's post.
- Bulk delete methods run until the relevant profile surface is exhausted; they no longer expose a caller-supplied item limit.

## Data Model

`ObservedPostData` contains:

- `platform_post_id`
- `author`
- `text`
- `raw`

Convenience helpers:

- `.metrics`: returns `raw["metrics"]` when present
- `.to_dict()`: returns a serializable copy

## Settings

`ControllerSettings` accepts:

- another `ControllerSettings`
- a plain object with matching attributes
- a mapping/dict with matching keys

Use `ControllerSettings.to_dict()` when you need to persist or inspect the active values.
