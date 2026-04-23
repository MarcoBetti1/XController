from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, MagicMock

try:
    from XController import XTextAdapter
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
    from XController import XTextAdapter


class NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class FakeSyncContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeSyncPlaywright:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.adapter = XTextAdapter(profile_path=str(Path(self.tmp.name) / "profile"))
        self.adapter.human = NoopHuman()

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    async def test_start_uses_sync_fallback_when_preferred(self) -> None:
        async def fake_start_sync_fallback() -> None:
            self.adapter._sync_context = FakeSyncContext()
            self.adapter._sync_page = object()
            self.adapter._sync_mode = True

        self.adapter._prefer_sync_playwright = MagicMock(return_value=True)
        self.adapter._start_sync_fallback = AsyncMock(side_effect=fake_start_sync_fallback)
        self.adapter._wait_network_idle = AsyncMock()

        await self.adapter.start()

        self.adapter._start_sync_fallback.assert_awaited_once()
        self.adapter._wait_network_idle.assert_awaited_once()
        self.assertTrue(self.adapter._sync_mode)
        self.assertIs(self.adapter.context, self.adapter._sync_context)
        self.assertIs(self.adapter.page, self.adapter._sync_page)

    async def test_close_cleans_up_sync_mode_resources(self) -> None:
        sync_context = FakeSyncContext()
        sync_playwright = FakeSyncPlaywright()
        self.adapter.context = sync_context  # type: ignore[assignment]
        self.adapter.page = object()  # type: ignore[assignment]
        self.adapter._sync_context = sync_context
        self.adapter._sync_page = object()
        self.adapter._sync_playwright = sync_playwright
        self.adapter._sync_mode = True

        async def run_sync(func, *args, **kwargs):
            return func(*args, **kwargs)

        self.adapter._run_sync = AsyncMock(side_effect=run_sync)

        await self.adapter.close()

        self.assertTrue(sync_context.closed)
        self.assertTrue(sync_playwright.stopped)
        self.assertIsNone(self.adapter.context)
        self.assertIsNone(self.adapter.page)
        self.assertFalse(self.adapter._sync_mode)
        self.assertIsNone(self.adapter._executor)


if __name__ == "__main__":
    unittest.main()
