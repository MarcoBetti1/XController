# Changelog

All notable changes for this package should be tracked here.

## Unreleased - 2026-03-14

### Added

- Added delete support for individual own posts, replies, and reposts.
- Added bulk cleanup helpers for posts, replies, reposts, and all content.
- Added `docs/API.md` with a clearer method inventory and compatibility notes.
- Added `docs/ARCHITECTURE.md` to document the internal structure and extension direction.
- Added `ObservedPostData.metrics` and `ObservedPostData.to_dict()` helpers.
- Added `ControllerSettings.to_dict()` and support for constructing settings from mappings/dicts.
- Added `__version__` export in `__init__.py`.
- Added a small unit-test suite for pure helpers and parsing logic.

### Changed

- Extended the lab manager/UI so destructive delete flows can be exercised from the existing manual harness.
- Rewrote the top-level `README.md` to describe the package as a reusable library rather than a single workflow script.
- Declared explicit setuptools package mapping so the flat package layout installs correctly as `x_controller`.
- Consolidated reply/comment behavior into one shared internal implementation while keeping both public entry points.
- Centralized username/post-id normalization in the adapter to reduce repeated parsing logic.
- Removed caller-provided `max_items` limits from the public `delete_all_*` methods so bulk cleanup always runs until the surface is exhausted.

### Fixed

- Added ownership verification before deleting authored content so a provided URL/post id cannot delete another account's post by mistake.
- Changed single-item delete flows to act from the profile timeline card instead of relying on the status page layout, matching the live X delete sequence more closely.
- Changed bulk delete flows to stop only after reaching the bottom of the profile surface instead of using a shallow retry limit, which better handles non-deletable cards mixed into posts/replies.
- Improved reply detection on `with_replies` cards by checking DOM reply markers and multiple status links, reducing skipped visible replies.
- Fixed profile-page detection in `current_state()` by correcting the X profile URL regex.
- Fixed sync typing behavior to click/focus the target element before keyboard input.
- Fixed Playwright executor cleanup during `close()` so sync fallback sessions do not leave the worker alive unnecessarily.
- Removed an unused duplicate metric-extraction helper.

### Migration Notes

- No public adapter methods were removed.
- Prefer `reply_to_post()` over `comment_post()` in new code.
- New bulk delete helpers return the deleted URLs so downstream callers can log or reconcile what was removed.
- `delete_all_posts()`, `delete_all_replies()`, `delete_all_reposts()`, and `delete_all_content()` no longer accept limit arguments.
- Use `pip install -e .` from inside this folder, or `pip install -e ./x_controller` from its parent directory.
- If downstream code serialized `ObservedPostData` manually, `to_dict()` can replace that custom code.
