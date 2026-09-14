"""Run a bundled AnimeJaNai ONNX model through DirectML."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--models", required=True)
    args = parser.parse_args()

    import onnxruntime as ort

    model_path = Path(args.models) / f"{args.model}.onnx"
    if not model_path.is_file():
        raise FileNotFoundError(f"AnimeJaNai model not found: {model_path}")

    session = ort.InferenceSession(str(model_path), providers=["DmlExecutionProvider"])
    with Image.open(args.input) as image:
        rgb = image.convert("RGB")
        array = np.asarray(rgb, dtype=np.float32) / 255.0
    tensor = np.transpose(array, (2, 0, 1))[None].astype(np.float16)
    output = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    output = np.transpose(output, (1, 2, 0))
    result = Image.fromarray(np.clip(output * 255.0, 0, 255).astype(np.uint8), "RGB")
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    result.save(args.output, "PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
