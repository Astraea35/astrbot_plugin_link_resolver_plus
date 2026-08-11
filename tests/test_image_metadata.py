"""Regression tests for FFmpeg metadata argument generation."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


if "PIL" not in sys.modules:
    pil_module = types.ModuleType("PIL")
    pil_module.ExifTags = types.SimpleNamespace(GPSTAGS={}, TAGS={})
    pil_module.Image = types.SimpleNamespace()
    sys.modules["PIL"] = pil_module


METADATA_PATH = (
    Path(__file__).resolve().parents[1] / "core" / "common" / "media" / "metadata.py"
)
SPEC = importlib.util.spec_from_file_location("image_metadata", METADATA_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load the image metadata module")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ImageMetadataStore = MODULE.ImageMetadataStore


class TestImageMetadataStore(unittest.TestCase):
    def test_ffmpeg_args_bound_large_metadata_values(self):
        oversized_value = "x" * 100_000
        metadata = {
            "source": {"title": oversized_value, "url": oversized_value},
            "original": {
                "filename": oversized_value,
                "capture": {},
                "copyright": {"description": oversized_value},
            },
        }

        args = ImageMetadataStore().ffmpeg_args(metadata, {"operation": "preview"})

        self.assertLess(sum(len(argument) + 1 for argument in args), 10_000)
        self.assertIn(f"title={'x' * 512}", args)
        comment = next(argument for argument in args if argument.startswith("comment="))
        payload = json.loads(comment.removeprefix("comment=astrbot-image-metadata:"))
        self.assertEqual(len(payload["source"]["title"]), 96)


if __name__ == "__main__":
    unittest.main(verbosity=2)