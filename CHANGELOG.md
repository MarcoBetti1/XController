# Changelog

Use this file for the next feature or release notes.

## Unreleased

- Add service-integration data models: `ActionResult`, `ActionPreflight`, `TimelineReadResult`, `MediaPreflight`, `ControllerHealth`, and `ObservedMediaData`.
- Add detailed action methods, action preflight, media preflight, thread-context reads, health checks, and debug snapshots for long-running browser services.
- Add timeline tab control with detailed `For You` / `Following` read reporting.
- Add explicit Playwright runtime controls through `playwright_mode` and `prefer_sync_playwright`.
- Split selector tables and soft-failure diagnostics into dedicated internal modules.
- Add `ActionFailureInfo`, `UIActionError`, and `strict_ui_failures` for observable or fail-fast UI automation errors.
- Add runtime lifecycle tests and expand CI to Ubuntu and Windows runners.
- Detect author-limited reply notices such as "Only some accounts can reply" in read results.
- Add recent and unread notification reads.
- Add local image attachments for new posts, replies, and quote posts.
