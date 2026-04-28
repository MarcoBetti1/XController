# Proposed Cleanup & Improvement Plan (XController)

## 1. Method Consolidation (Removing Redundancy)
Currently, several methods act as thin wrappers or duplicates of others. Consolidating these will shrink the API surface without losing any functionality.

*   **Combine `read_notifications` & `read_unread_notifications`**
    *   *Change:* Remove `read_unread_notifications`.
    *   *Reason:* `read_notifications(limit=20, unread_only=True)` handles this natively. Having two methods is redundant.
*   **Combine `recover_home` & `refresh_home`**
    *   *Change:* Deprecate both in favor of a single `return_home(force_refresh: bool = False)`.
    *   *Reason:* They do the exact same conceptual action. A single boolean parameter makes the developer's intent clearer.
*   **Remove `comment_post` Alias**
    *   *Change:* Remove `comment_post` and strictly enforce `reply_to_post`.
    *   *Reason:* X terminology canonical uses "reply." Reducing aliases reduces confusion for downstream developers.
*   **Consolidate Media Convenience Wrappers**
    *   *Change:* Consider deprecating `post_image`, `reply_with_image`, and `quote_post_with_image`.
    *   *Reason:* The core methods (`post_text`, `reply_to_post`, `quote_post`) already accept an `image_paths` argument.

## 2. Timeline Navigation Behavior (Confirmed & Tested)
*   *Current State:* `read_timeline` and `read_following_timeline` gracefully check current UI state via `settle_home()`. They **do not** hard-refresh if already on the correct tab.
*   *Proposed Improvement:* The `reset_scroll=True` behavior (pressing the "Home" key to go to the top of the feed) is excellent, but we should make sure downstream services understand that passing `force_refresh=True` forces a full UI reload, whereas `reset_scroll=True` just fetches the newest visible items at the top of the DOM.

## 3. Standardize Return Types (Moving away from Booleans)
*   *Change:* Currently, methods like `like_post` or `delete_post` return a simple `bool`. However, you already have `*_detailed` variants (e.g., `like_post_detailed`) that return an `ActionResult` with rich failure diagnostics (`failure_reason`, `failure_stage`).
*   *Action:* Phase out the `bool` returning methods entirely in a future major version (v2.0), making the `*_detailed` methods the default. This prevents callers from losing critical Playwright trace information when UI actions fail.

## 4. Update the Base Interface (`base.py`)
*   When removing `read_unread_notifications`, `refresh_home`, `recover_home`, and `comment_post`, ensure `SocialPlatformAdapter` in `base.py` is updated to match the new strict contract.