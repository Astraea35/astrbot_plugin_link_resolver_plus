"""Run a bundled AnimeJaNai ONNX model through DirectML or CPU.

The bundled AnimeJaNai weights are native 2x models with dynamic spatial
dimensions. Large inputs can therefore be processed as overlapping tiles
without changing their output geometry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def _create_session(ort, model_path: Path, *, provider: str):
    """Create a DirectML-safe, sequential ONNX Runtime session."""
    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_mem_pattern = False

    providers = ["CPUExecutionProvider"]
    if provider == "directml":
        providers = ["DmlExecutionProvider", "CPUExecutionProvider"]

    return ort.InferenceSession(
        str(model_path),
        sess_options=options,
        providers=providers,
    )


def _load_preferred_session(ort, model_path: Path):
    if "DmlExecutionProvider" in ort.get_available_providers():
        try:
            return _create_session(ort, model_path, provider="directml"), "DmlExecutionProvider"
        except Exception as exc:
            print(f"AnimeJaNai DirectML session failed, falling back to CPU: {exc}", file=sys.stderr)

    return _create_session(ort, model_path, provider="cpu"), "CPUExecutionProvider"


def _image_to_tensor(image: Image.Image) -> np.ndarray:
    array = np.asarray(image, dtype=np.float32) / 255.0
    tensor = np.transpose(array, (2, 0, 1))[None].astype(np.float16)
    del array
    return tensor


def _infer_image(session, image: Image.Image) -> np.ndarray:
    """Infer one RGB image and return its HWC uint8 output."""
    tensor = _image_to_tensor(image)
    try:
        output = session.run(None, {session.get_inputs()[0].name: tensor})[0]
    finally:
        del tensor

    if output.ndim != 4 or output.shape[0] != 1 or output.shape[1] != 3:
        raise ValueError(f"Unexpected AnimeJaNai output shape: {output.shape}")

    pixels = np.transpose(output[0], (1, 2, 0))
    del output
    return np.clip(pixels * 255.0, 0, 255).astype(np.uint8)


def _infer_tiled(session, image: Image.Image, tile_size: int, overlap: int) -> np.ndarray:
    """Run sequential overlapping tiles and crop each tile to its core region.

    The model receives the surrounding ``overlap`` pixels as context. Only
    the unpadded core is copied into the destination, avoiding seams without
    retaining more than one tile result in memory.
    """
    width, height = image.size
    destination: np.ndarray | None = None
    scale_x: int | None = None
    scale_y: int | None = None

    for core_y0 in range(0, height, tile_size):
        core_y1 = min(height, core_y0 + tile_size)
        source_y0 = max(0, core_y0 - overlap)
        source_y1 = min(height, core_y1 + overlap)

        for core_x0 in range(0, width, tile_size):
            core_x1 = min(width, core_x0 + tile_size)
            source_x0 = max(0, core_x0 - overlap)
            source_x1 = min(width, core_x1 + overlap)

            tile = image.crop((source_x0, source_y0, source_x1, source_y1))
            tile_pixels = _infer_image(session, tile)
            tile_height, tile_width = tile.size[1], tile.size[0]
            output_height, output_width = tile_pixels.shape[:2]

            if output_height % tile_height or output_width % tile_width:
                raise ValueError(
                    "AnimeJaNai tiled output does not preserve an integer scale: "
                    f"{tile_pixels.shape} for input {tile.size}"
                )

            current_scale_x = output_width // tile_width
            current_scale_y = output_height // tile_height
            if current_scale_x != current_scale_y or current_scale_x < 1:
                raise ValueError(
                    "AnimeJaNai tiled output has inconsistent spatial scale: "
                    f"{tile_pixels.shape} for input {tile.size}"
                )

            if destination is None:
                scale_x = current_scale_x
                scale_y = current_scale_y
                destination = np.empty((height * scale_y, width * scale_x, 3), dtype=np.uint8)
            elif current_scale_x != scale_x or current_scale_y != scale_y:
                raise ValueError("AnimeJaNai scale changed between tiles")

            assert destination is not None and scale_x is not None and scale_y is not None
            crop_x0 = (core_x0 - source_x0) * scale_x
            crop_x1 = crop_x0 + (core_x1 - core_x0) * scale_x
            crop_y0 = (core_y0 - source_y0) * scale_y
            crop_y1 = crop_y0 + (core_y1 - core_y0) * scale_y
            destination[
                core_y0 * scale_y : core_y1 * scale_y,
                core_x0 * scale_x : core_x1 * scale_x,
            ] = tile_pixels[crop_y0:crop_y1, crop_x0:crop_x1]

    if destination is None:
        raise ValueError("AnimeJaNai cannot infer an empty image")
    return destination


def _infer(session, image: Image.Image, tile_size: int, tile_overlap: int) -> np.ndarray:
    if tile_size <= 0 or (image.width <= tile_size and image.height <= tile_size):
        return _infer_image(session, image)
    return _infer_tiled(session, image, tile_size, tile_overlap)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument(
        "--tile-size",
        type=int,
        default=0,
        help="Input tile edge in pixels; 0 runs the whole image in one inference.",
    )
    parser.add_argument(
        "--tile-overlap",
        type=int,
        default=32,
        help="Input context on every tiled edge in pixels.",
    )
    args = parser.parse_args()

    if args.tile_size < 0:
        parser.error("--tile-size must be 0 or greater")
    if args.tile_size > 0 and (args.tile_overlap < 0 or args.tile_overlap >= args.tile_size):
        parser.error("--tile-overlap must be at least 0 and smaller than --tile-size")

    import onnxruntime as ort

    model_path = Path(args.models) / f"{args.model}.onnx"
    if not model_path.is_file():
        raise FileNotFoundError(f"AnimeJaNai model not found: {model_path}")

    with Image.open(args.input) as image:
        rgb = image.convert("RGB")

    session, active_provider = _load_preferred_session(ort, model_path)
    try:
        pixels = _infer(session, rgb, args.tile_size, args.tile_overlap)
    except Exception as exc:
        if active_provider != "DmlExecutionProvider":
            raise
        print(f"AnimeJaNai DirectML inference failed, retrying on CPU: {exc}", file=sys.stderr)
        session = _create_session(ort, model_path, provider="cpu")
        pixels = _infer(session, rgb, args.tile_size, args.tile_overlap)

    result = Image.fromarray(pixels, "RGB")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    result.save(args.output, "PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
