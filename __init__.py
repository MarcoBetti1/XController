from importlib.metadata import PackageNotFoundError, version

from .adapter import XController, XTextAdapter
from ._diagnostics import ActionFailureInfo, UIActionError
from .base import ObservedNotificationData, ObservedPostData, SocialPlatformAdapter
from .settings import ControllerSettings

try:
    __version__ = version("XController")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "ActionFailureInfo",
    "ControllerSettings",
    "ObservedNotificationData",
    "ObservedPostData",
    "SocialPlatformAdapter",
    "UIActionError",
    "XController",
    "XTextAdapter",
    "__version__",
]
