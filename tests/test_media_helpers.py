from __future__ import annotations

import pytest

from x_controller import XTextAdapter


def make_adapter(tmp_path):
    return XTextAdapter(profile_path=str(tmp_path / "profile"))


def close_adapter(adapter: XTextAdapter) -> None:
    adapter._shutdown_executor()


def test_normalize_image_paths_accepts_single_common_image(tmp_path):
    adapter = make_adapter(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"not a real image; path validation only")
    try:
        assert adapter._normalize_image_paths(str(image)) == [str(image.resolve())]
    finally:
        close_adapter(adapter)


def test_normalize_image_paths_accepts_multiple_images(tmp_path):
    adapter = make_adapter(tmp_path)
    images = []
    for name in ("one.jpg", "two.webp"):
        image = tmp_path / name
        image.write_bytes(b"image")
        images.append(image)
    try:
        assert adapter._normalize_image_paths(images) == [str(path.resolve()) for path in images]
    finally:
        close_adapter(adapter)


def test_normalize_image_paths_rejects_unsupported_extension(tmp_path):
    adapter = make_adapter(tmp_path)
    image = tmp_path / "sample.bmp"
    image.write_bytes(b"image")
    try:
        with pytest.raises(ValueError, match="Unsupported image extension"):
            adapter._normalize_image_paths(image)
    finally:
        close_adapter(adapter)


def test_normalize_image_paths_rejects_more_than_four_images(tmp_path):
    adapter = make_adapter(tmp_path)
    images = []
    for idx in range(5):
        image = tmp_path / f"{idx}.png"
        image.write_bytes(b"image")
        images.append(image)
    try:
        with pytest.raises(ValueError, match="up to 4 images"):
            adapter._normalize_image_paths(images)
    finally:
        close_adapter(adapter)
