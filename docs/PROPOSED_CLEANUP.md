# Proposed Cleanup Plan (Archived Status)

This file is kept for historical context. The original cleanup plan has been reviewed against the current documented/public behavior.

## Status Summary

| Original plan item | Status | Notes |
| --- | --- | --- |
| Remove `read_unread_notifications` in favor of `read_notifications(unread_only=True)` | Implemented | Reflected in API docs and changelog; unread reads are documented through `read_notifications(unread_only=True)`. |
| Replace `recover_home` / `refresh_home` with `return_home(force_refresh=False)` | Implemented | Current docs describe `return_home(force_refresh=False)` as the canonical home recovery entrypoint. |
| Remove `comment_post` in favor of `reply_to_post` | Implemented | Current docs describe `reply_to_post()` as canonical and note `comment_post()` removal. |
| Consolidate image convenience wrappers | Partially implemented (deprecation path active) | `post_image`, `reply_with_image`, and `quote_post_with_image` are still present as deprecated compatibility wrappers. |
| Clarify `force_refresh` vs `reset_scroll` timeline behavior | Implemented in docs | Behavior is documented in `docs/API.md` for timeline reads and post-action settling. |
| Move from boolean methods to `*_detailed()` action results | Active future direction | Still a compatibility migration path; no removal is planned in this docs cleanup update. |
| Keep base contract aligned with cleanup choices | Implemented | Public documentation describes the consolidated method names and compatibility surface. |

## Current Source of Truth

Use these docs for current behavior instead of this historical plan:

- [Public API contract](PUBLIC_CONTRACT.md)
- [API notes](API.md)
- [Change log](../CHANGELOG.md)
