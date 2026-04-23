from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

try:
    from XController import ControllerSettings, UIActionError, XTextAdapter
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
    from XController import ControllerSettings, UIActionError, XTextAdapter


class NoopHuman:
    async def jitter(self, *_args, **_kwargs) -> None:
        return None


class PageStub:
    def __init__(self, url: str):
        self.url = url


class SoftFailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_adapter(self, *, strict_ui_failures: bool = False) -> XTextAdapter:
        adapter = XTextAdapter(
            profile_path=str(Path(self.tmp.name) / "profile"),
            settings=ControllerSettings(strict_ui_failures=strict_ui_failures),
        )
        adapter.human = NoopHuman()
        adapter.page = PageStub("https://x.com/home")  # type: ignore[assignment]
        adapter._looks_like_home_timeline = AsyncMock(return_value=True)
        adapter._find_first = AsyncMock(side_effect=[None, object()])
        adapter._click = AsyncMock()
        adapter._clear_input_like_human = AsyncMock()
        adapter._keyboard_press = AsyncMock()
        adapter._wait_network_idle = AsyncMock()
        return adapter

    async def test_open_search_query_records_soft_failure_details(self) -> None:
        adapter = self._make_adapter()
        adapter._type_text = AsyncMock(side_effect=RuntimeError("search box vanished"))

        result = await adapter._open_search_query("browser automation")

        self.assertFalse(result)
        self.assertIsNotNone(adapter.last_action_error)
        assert adapter.last_action_error is not None
        self.assertEqual(adapter.last_action_error.action, "open_search_query")
        self.assertEqual(adapter.last_action_error.error_type, "RuntimeError")
        self.assertIn("search box vanished", adapter.last_action_error.message)
        state = await adapter.current_state()
        self.assertEqual(state["last_action_error"], adapter.last_action_error.summary)

    async def test_open_search_query_raises_ui_action_error_in_strict_mode(self) -> None:
        adapter = self._make_adapter(strict_ui_failures=True)
        adapter._type_text = AsyncMock(side_effect=RuntimeError("search box vanished"))

        with self.assertRaises(UIActionError) as ctx:
            await adapter._open_search_query("browser automation")

        self.assertEqual(ctx.exception.failure.action, "open_search_query")
        self.assertEqual(ctx.exception.failure.error_type, "RuntimeError")

    async def test_open_search_query_reraises_programmer_errors(self) -> None:
        adapter = self._make_adapter()
        adapter._type_text = AsyncMock(side_effect=TypeError("bad helper wiring"))

        with self.assertRaises(TypeError):
            await adapter._open_search_query("browser automation")

        self.assertIsNone(adapter.last_action_error)


if __name__ == "__main__":
    unittest.main()
