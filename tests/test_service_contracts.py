from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

try:
    from XController import AccountStats, ActionPreflight, ActionResult, ControllerSettings, MediaPreflight, TimelineReadResult, XTextAdapter
except ModuleNotFoundError as exc:
    if exc.name != "XController":
        raise
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "XController",
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise
    module = importlib.util.module_from_spec(spec)
    sys.modules["XController"] = module
    spec.loader.exec_module(module)
    from XController import AccountStats, ActionPreflight, ActionResult, ControllerSettings, MediaPreflight, TimelineReadResult, XTextAdapter


class ServiceContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adapter(self, settings: ControllerSettings | dict | None = None) -> XTextAdapter:
        adapter = XTextAdapter(profile_path=str(self.tmp_path / "profile"), settings=settings)
        self.addCleanup(adapter._shutdown_executor)
        return adapter

    def test_action_models_serialize_to_plain_dicts(self) -> None:
        result = ActionResult(ok=False, action="reply", target_post_id="123", failure_reason="reply_limited")
        preflight = ActionPreflight(ok=False, action="reply", target_post_id="123", reason="reply_limited")
        timeline = TimelineReadResult(posts=[], requested_tab="for_you", active_tab="following", source_url="", current_state="home", raw_count=0, article_count=0)
        media = MediaPreflight(ok=False, errors=[{"path": "x.bmp", "reason": "unsupported_extension"}])
        account = AccountStats(handle="example", followers=10, raw={"source": "test"})

        self.assertEqual(result.to_dict()["failure_reason"], "reply_limited")
        self.assertEqual(preflight.to_dict()["reason"], "reply_limited")
        self.assertEqual(timeline.to_dict()["active_tab"], "following")
        self.assertEqual(media.to_dict()["errors"][0]["reason"], "unsupported_extension")
        self.assertEqual(account.to_dict()["followers"], 10)
        self.assertEqual(account.to_dict()["raw"]["source"], "test")

    def test_account_stats_payload_normalizes_profile_counts(self) -> None:
        adapter = self._adapter()
        payload = {
            "current_url": "https://x.com/example",
            "title": "Example User (@example) / X",
            "user_name_blocks": ["Example User\n@example\n1.2K posts"],
            "bio": "Building things",
            "location": "Chicago, IL",
            "joined_at": "Joined January 2024",
            "verified": True,
            "links": [
                {"href": "https://x.com/example/following", "text": "1,234 Following"},
                {"href": "https://x.com/example/verified_followers", "text": "5.6K Followers"},
            ],
        }

        stats = adapter._account_stats_from_payload("example", payload, "2026-04-24T00:00:00+00:00", {"warnings": []})

        self.assertEqual(stats.handle, "example")
        self.assertEqual(stats.display_name, "Example User")
        self.assertEqual(stats.followers, 5600)
        self.assertEqual(stats.following, 1234)
        self.assertEqual(stats.posts, 1200)
        self.assertTrue(stats.verified)
        self.assertEqual(stats.bio, "Building things")
        self.assertIn("likes_count_unavailable", stats.raw["warnings"])

    async def test_account_stats_invalid_explicit_handle_returns_structured_error(self) -> None:
        adapter = self._adapter()

        stats = await adapter.account_stats("bad handle")

        self.assertEqual(stats.handle, "")
        self.assertEqual(stats.raw["error"], "invalid_handle")

    def test_playwright_mode_overrides_sync_preference(self) -> None:
        self.assertFalse(self._adapter({"playwright_mode": "async"})._prefer_sync_playwright())
        self.assertTrue(self._adapter({"playwright_mode": "sync"})._prefer_sync_playwright())
        self.assertTrue(self._adapter({"playwright_mode": "async", "prefer_sync_playwright": True})._prefer_sync_playwright())

    async def test_attach_images_preflight_reports_file_errors(self) -> None:
        adapter = self._adapter()
        good = self.tmp_path / "ok.png"
        bad = self.tmp_path / "bad.bmp"
        good.write_bytes(b"image")
        bad.write_bytes(b"image")

        result = await adapter.attach_images_preflight([good, bad, self.tmp_path / "missing.jpg"])

        self.assertFalse(result.ok)
        self.assertEqual(result.normalized_paths, [str(good.resolve())])
        self.assertEqual([item["reason"] for item in result.errors], ["unsupported_extension", "file_not_found"])

    async def test_detailed_action_methods_return_structured_not_started_failures(self) -> None:
        adapter = self._adapter()

        reply = await adapter.reply_to_post_detailed("123", "hello")
        like = await adapter.like_post_detailed("123")
        post = await adapter.post_text_detailed("hello")

        self.assertFalse(reply.ok)
        self.assertEqual(reply.failure_reason, "page_not_started")
        self.assertFalse(like.ok)
        self.assertEqual(like.failure_stage, "not_started")
        self.assertFalse(post.ok)
        self.assertEqual(post.action, "post")


if __name__ == "__main__":
    unittest.main()
