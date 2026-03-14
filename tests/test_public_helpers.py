from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_PARENT = Path(__file__).resolve().parents[2]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

if "playwright.async_api" not in sys.modules:
    playwright_module = types.ModuleType("playwright")
    async_api_module = types.ModuleType("playwright.async_api")
    sync_api_module = types.ModuleType("playwright.sync_api")

    class _AsyncBrowserContext:
        pages: list[object] = []

    class _AsyncPage:
        url = ""

    class _SyncBrowserContext:
        pages: list[object] = []

    class _SyncPage:
        url = ""

    def _not_available(*_args, **_kwargs):
        raise RuntimeError("playwright_not_available_for_unit_test")

    async_api_module.BrowserContext = _AsyncBrowserContext
    async_api_module.Page = _AsyncPage
    async_api_module.async_playwright = _not_available
    sync_api_module.BrowserContext = _SyncBrowserContext
    sync_api_module.Page = _SyncPage
    sync_api_module.sync_playwright = _not_available

    playwright_module.async_api = async_api_module
    playwright_module.sync_api = sync_api_module

    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.async_api"] = async_api_module
    sys.modules["playwright.sync_api"] = sync_api_module

from x_controller import ControllerSettings, ObservedPostData, XController


class ControllerSettingsTests(unittest.TestCase):
    def test_from_any_accepts_mapping(self) -> None:
        settings = ControllerSettings.from_any(
            {
                "anti_bot_typing_min_ms": 12,
                "browser_width_min": 1440,
            }
        )

        self.assertEqual(settings.anti_bot_typing_min_ms, 12)
        self.assertEqual(settings.browser_width_min, 1440)

    def test_from_any_accepts_plain_object(self) -> None:
        source = SimpleNamespace(
            anti_bot_typing_max_ms=321,
            browser_height_max=999,
        )
        settings = ControllerSettings.from_any(source)

        self.assertEqual(settings.anti_bot_typing_max_ms, 321)
        self.assertEqual(settings.browser_height_max, 999)

    def test_to_dict_round_trips_known_keys(self) -> None:
        settings = ControllerSettings()
        data = settings.to_dict()

        self.assertEqual(data["default_user_agent"], settings.default_user_agent)
        self.assertEqual(data["browser_width_min"], settings.browser_width_min)


class ObservedPostDataTests(unittest.TestCase):
    def test_metrics_property_and_to_dict(self) -> None:
        post = ObservedPostData(
            platform_post_id="123",
            author="alice",
            text="hello",
            raw={"metrics": {"likes": 4}},
        )

        self.assertEqual(post.metrics["likes"], 4)
        self.assertEqual(post.to_dict()["platform_post_id"], "123")


class AdapterHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.adapter = XController(profile_path=self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_normalize_post_id(self) -> None:
        self.assertEqual(self.adapter._normalize_post_id("https://x.com/a/status/987654321"), "987654321")
        self.assertEqual(self.adapter._normalize_post_id("post 42"), "42")
        self.assertEqual(self.adapter._normalize_post_id("no-post-id"), "")

    def test_metric_parsing(self) -> None:
        metrics = self.adapter._extract_metrics_from_text("1.2K views 75 Likes 3 replies 2 reposts")

        self.assertEqual(metrics["views"], 1200)
        self.assertEqual(metrics["likes"], 75)
        self.assertEqual(metrics["replies"], 3)
        self.assertEqual(metrics["comments"], 3)
        self.assertEqual(metrics["reposts"], 2)

    def test_profile_url_regex_matches_normal_profile_page(self) -> None:
        match = self.adapter.PROFILE_URL_RE.search("https://x.com/example_user")

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "example_user")

    def test_extract_profile_handle_from_href(self) -> None:
        self.assertEqual(self.adapter._extract_profile_handle_from_href("https://x.com/example_user"), "example_user")
        self.assertEqual(self.adapter._extract_profile_handle_from_href("/example_user"), "example_user")
        self.assertEqual(self.adapter._extract_profile_handle_from_href("/home"), "")

    def test_profile_item_kind_matching(self) -> None:
        own_handle = "marco"

        self.assertTrue(
            self.adapter._profile_item_matches_kind(
                {"author": "marco", "is_reply": False, "is_repost": False},
                "post",
                own_handle,
            )
        )
        self.assertTrue(
            self.adapter._profile_item_matches_kind(
                {"author": "marco", "is_reply": True, "is_repost": False},
                "reply",
                own_handle,
            )
        )
        self.assertTrue(
            self.adapter._profile_item_matches_kind(
                {"author": "someone_else", "is_reply": False, "is_repost": True},
                "repost",
                own_handle,
            )
        )
        self.assertFalse(
            self.adapter._profile_item_matches_kind(
                {"author": "someone_else", "is_reply": False, "is_repost": False},
                "post",
                own_handle,
            )
        )


if __name__ == "__main__":
    unittest.main()
