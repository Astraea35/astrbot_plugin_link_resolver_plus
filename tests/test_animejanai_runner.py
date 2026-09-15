"""Unit tests for AnimeJaNai tiling and ONNX Runtime session setup."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import numpy as np
from PIL import Image


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "common"
    / "media"
    / "animejanai_runner.py"
)
SPEC = importlib.util.spec_from_file_location("animejanai_runner_under_test", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class Nearest2xSession:
    class _Input:
        name = "pixel_values"

    def get_inputs(self):
        return [self._Input()]

    def run(self, _output_names, inputs):
        tensor = inputs["pixel_values"].astype(np.float32)
        return [np.repeat(np.repeat(tensor, 2, axis=2), 2, axis=3)]


class FakeSessionOptions:
    def __init__(self):
        self.execution_mode = None
        self.enable_mem_pattern = True


class FakeOrt:
    class ExecutionMode:
        ORT_SEQUENTIAL = object()

    def __init__(self):
        self.options = None
        self.path = None
        self.providers = None
        self.session = object()

    SessionOptions = FakeSessionOptions

    def InferenceSession(self, path, *, sess_options, providers):
        self.path = path
        self.options = sess_options
        self.providers = providers
        return self.session


class AnimeJaNaiRunnerTests(unittest.TestCase):
    def test_overlapping_tiles_rebuild_exact_full_image_nearest_2x_output(self):
        pixels = np.zeros((9, 13, 3), dtype=np.uint8)
        pixels[:, :, 0] = np.arange(13, dtype=np.uint8)
        pixels[:, :, 1] = np.arange(9, dtype=np.uint8)[:, None]
        pixels[:, :, 2] = (
            pixels[:, :, 0].astype(np.uint16) * 17
            + pixels[:, :, 1].astype(np.uint16) * 29
        ) % 256
        image = Image.fromarray(pixels, "RGB")
        session = Nearest2xSession()

        whole_image = runner._infer(session, image, tile_size=0, tile_overlap=0)
        tiled = runner._infer(session, image, tile_size=5, tile_overlap=2)

        self.assertEqual(tiled.shape, (18, 26, 3))
        np.testing.assert_array_equal(tiled, whole_image)

    def test_directml_session_is_sequential_and_disables_memory_pattern(self):
        ort = FakeOrt()
        model_path = Path("C:/models/animejanai-v3.1-balanced.onnx")

        session = runner._create_session(ort, model_path, provider="directml")

        self.assertIs(session, ort.session)
        self.assertEqual(ort.path, str(model_path))
        self.assertIs(ort.options.execution_mode, ort.ExecutionMode.ORT_SEQUENTIAL)
        self.assertFalse(ort.options.enable_mem_pattern)
        self.assertEqual(
            ort.providers,
            ["DmlExecutionProvider", "CPUExecutionProvider"],
        )


if __name__ == "__main__":
    unittest.main()
