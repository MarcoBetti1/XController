from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

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


class MediaHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.adapter = XTextAdapter(profile_path=str(self.tmp_path / "profile"))

    def tearDown(self) -> None:
        self.adapter._shutdown_executor()
        self.tmp.cleanup()

    def test_normalize_image_paths_accepts_single_common_image(self) -> None:
        image = self.tmp_path / "sample.png"
        image.write_bytes(b"not a real image; path validation only")

        self.assertEqual(
            self.adapter._normalize_image_paths(str(image)),
            [str(image.resolve())],
        )

    def test_normalize_image_paths_accepts_multiple_images(self) -> None:
        images = []
        for name in ("one.jpg", "two.webp"):
            image = self.tmp_path / name
            image.write_bytes(b"image")
            images.append(image)

        self.assertEqual(
            self.adapter._normalize_image_paths(images),
            [str(path.resolve()) for path in images],
        )

    def test_normalize_image_paths_rejects_unsupported_extension(self) -> None:
        image = self.tmp_path / "sample.bmp"
        image.write_bytes(b"image")

        with self.assertRaisesRegex(ValueError, "Unsupported image extension"):
            self.adapter._normalize_image_paths(image)

    def test_normalize_image_paths_rejects_more_than_four_images(self) -> None:
        images = []
        for idx in range(5):
            image = self.tmp_path / f"{idx}.png"
            image.write_bytes(b"image")
            images.append(image)

        with self.assertRaisesRegex(ValueError, "up to 4 images"):
            self.adapter._normalize_image_paths(images)


if __name__ == "__main__":
    unittest.main()
