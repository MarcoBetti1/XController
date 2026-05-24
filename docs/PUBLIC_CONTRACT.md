# Public API Contract

This document defines what downstream projects should treat as the supported public surface for `XController`.

## Import Contract

Import from the package root:

```python
from XController import XController, SyncXController, ControllerSettings
```

Do **not** import from implementation modules such as `adapter`, `sync`, `_ui_selectors`, `_diagnostics`, `human`, or other underscored/internal modules.

## Supported Public Surface

Primary controller exports:

- `XController`
- `XTextAdapter` (compatibility alias of `XController`)
- `SyncXController`
- `XControllerService` (compatibility alias of `SyncXController`)

Settings and data/diagnostic exports:

- `ControllerSettings`
- `AccountStats`
- `ActionFailureInfo`
- `ActionPreflight`
- `ActionResult`
- `ControllerHealth`
- `LoginState`
- `MediaCaptureData`
- `MediaPreflight`
- `ObservedMediaData`
- `ObservedNotificationData`
- `ObservedPostData`
- `TimelineReadResult`
- `UIActionError`

## Compatibility Aliases and Deprecated Methods

Current compatibility behavior that downstream users may still encounter:

- `XTextAdapter` is retained as a class alias of `XController`.
- `XControllerService` is retained as an alias of `SyncXController`.
- `return_home(force_refresh=False)` is the canonical home recovery method.
- `read_notifications(unread_only=True)` is the canonical unread-notification path.
- `reply_to_post()` is the canonical reply method.
- `post_image()`, `reply_with_image()`, and `quote_post_with_image()` remain as deprecated wrappers over image-capable core methods.
- Legacy compact boolean-returning action methods remain available for compatibility; `*_detailed()` methods are recommended for richer diagnostics.

## Non-Contract Internals

The following are implementation details and may change without compatibility guarantees:

- selector tables and DOM heuristics in `_ui_selectors.py`
- internal action-failure wiring in `_diagnostics.py`
- low-level browser interaction helpers inside `adapter.py` and `human.py`
- threading/event-loop internals used by the sync facade

For method-level behavior details, see [API Notes](API.md).
