# Hybrid classifier model

The v1 classifier asset is derived from the MIT-licensed
`laion/CLIP-ViT-B-32-laion2B-s34B-b79K` model and the corresponding
`onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX` conversion.

Only the image branch is shipped. The offline export extracts the ONNX subgraph
from `pixel_values` to `image_embeds`, changes the declared image input type to
FP16 to match the converted convolution weights, and validates the resulting
graph with the ONNX checker. The final model is 175,981,046 bytes, below the
220MB target, and does not require Torch or Transformers at runtime.

Text embeddings are generated offline from six English prompts for each class:
`anime`, `photo`, and `text_ui`. Embeddings are L2-normalized, averaged by class,
normalized again, and stored in `clip_prototypes.json`. The prompt strings remain
in that file so the prototypes are auditable and reproducible.

## Release assets

The `classifier-v1` GitHub Release contains:

- `clip_vision_fp16.onnx`
- `clip_prototypes.json`
- `CLIP_LICENSE.txt`
- `SHA256SUMS.txt`

Expected SHA256 values:

```text
013f803271d6264f6f993ec3ca2597426810ed52b0a9855732239b02bda3dd3a  clip_vision_fp16.onnx
c6cad2d9c1dd36da471000d95c155fce535f62af2709a9d9860c6d8516a104fe  clip_prototypes.json
f3623d45c2b6f37fc3d3e129d53275f8d65490915bec41b8cf7b66367d00d607  CLIP_LICENSE.txt
```

The exact asset hashes and the SHA256 of `SHA256SUMS.txt`
(`ad2b93d4247dcdd0c5ab977ac97566ced13160b77eae16593197922e7063f108`)
are pinned in the plugin source. A replacement manifest from the same download
origin therefore cannot authorize different model bytes.

Downloads use a directory lock and uniquely named `.part` files so plugin
reloads cannot overwrite each other's temporary data. Each verified asset is
then atomically moved into the persistent instance directory. Model download or
load failure leaves Rule V2 available and does not interrupt media parsing.

## Local evaluation

The classifier was exercised locally on 400 cached platform images without
uploading any sample. On an RTX 5060 8GB system:

- Rule V2 processed the set in 58.334 seconds. Its initial distribution was 28
  `anime`, 18 `photo`, and 354 `uncertain`.
- CLIP successfully reviewed 347 images. The final distribution was 364
  `anime`, 24 `photo`, 1 `text_ui`, and 11 `uncertain`.
- Cold DirectML session load took 0.705 seconds. The full mixed batch took
  106.539 seconds, averaging 266.35 ms per image.
- Reported GPU memory was 4067MB before load, 4254MB after load, 4312MB at peak,
  and 4131MB after release.
- Three separately checked AstrBot/UI screenshots were all classified as
  `text_ui`.

The cached set does not have human-authored ground-truth labels. These results
verify the offline pipeline, DirectML stability, batching, and resource release,
but do not establish the target photo/anime/UI misclassification percentages.
A labeled local set is still required for a statistically valid error-rate
report.
