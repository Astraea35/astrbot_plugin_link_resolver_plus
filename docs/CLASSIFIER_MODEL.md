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
