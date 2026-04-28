from importlib.metadata import PackageNotFoundError, version

from .adapter import XController, XTextAdapter
from .sync import SyncXController, XControllerService
from ._diagnostics import ActionFailureInfo, UIActionError
from .base import (
    AccountStats,
    ActionPreflight,
    ActionResult,
    ControllerHealth,
    LoginState,
    MediaCaptureData,
    MediaPreflight,
    ObservedMediaData,
    ObservedNotificationData,
    ObservedPostData,
    SocialPlatformAdapter,
    TimelineReadResult,
)
from .settings import ControllerSettings

try:
    __version__ = version("XController")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "AccountStats",
    "ActionFailureInfo",
    "ActionPreflight",
    "ActionResult",
    "ControllerSettings",
    "ControllerHealth",
    "LoginState",
    "MediaCaptureData",
    "MediaPreflight",
    "ObservedMediaData",
    "ObservedNotificationData",
    "ObservedPostData",
    "SocialPlatformAdapter",
    "SyncXController",
    "TimelineReadResult",
    "UIActionError",
    "XController",
    "XControllerService",
    "XTextAdapter",
    "__version__",
]
