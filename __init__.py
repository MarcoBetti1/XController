from importlib.metadata import PackageNotFoundError, version

from .adapter import XController, XTextAdapter
from .base import ObservedPostData, SocialPlatformAdapter
from .settings import ControllerSettings

try:
    __version__ = version("x-controller")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = [
    "ControllerSettings",
    "ObservedPostData",
    "SocialPlatformAdapter",
    "XController",
    "XTextAdapter",
    "__version__",
]
