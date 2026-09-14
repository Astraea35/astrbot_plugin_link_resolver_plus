# Third-party notices

## F2

The Douyin guest-session fallback includes a request-signing implementation from
[Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2), licensed under the
Apache License 2.0.

Copyright and license notices for F2 remain with its respective authors. The
vendored implementation is in `core/douyin/abogus.py` and retains its original
header and attribution.

## AnimeJaNai

The optional AnimeJaNai V3.1 Balanced and Sharp model weights are attributed to
[the-database/mpv-AnimeJaNai](https://github.com/the-database/mpv-AnimeJaNai).
They are distributed under the Creative Commons Attribution-NonCommercial-
ShareAlike 4.0 International license (`CC BY-NC-SA 4.0`). They may be shared and
used for noncommercial purposes with attribution, preservation of the license,
indication of changes, and ShareAlike terms for adaptations.

The full license is included in the companion resource archive at
`resources/licenses/AnimeJaNai-LICENSE.txt`. This project claims no ownership of
the model weights. Commercial use requires separate permission from the model
rights holder.

## LAION CLIP ViT-B/32

The optional hybrid image classifier uses a vision-only FP16 ONNX subgraph
derived from `laion/CLIP-ViT-B-32-laion2B-s34B-b79K`. The upstream model card
declares the model under the MIT license. The ONNX source conversion is provided
by `onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX`.

The model, precomputed prompt prototypes, attribution, MIT license text, and
SHA256 manifest are distributed as separate GitHub Release assets and are not
committed to the plugin repository. Runtime downloads are stored in the AstrBot
instance-level `models/classifier` directory.

## SPAN NCNN Vulkan

The bundled SPAN NCNN Vulkan executable is based on
[TNTwise/SPAN-ncnn-vulkan](https://github.com/TNTwise/SPAN-ncnn-vulkan), licensed
under the GNU Affero General Public License v3.0. The complete license is included
at `resources/licenses/SPAN-ncnn-vulkan-LICENSE.txt`. When redistributing the
binary, retain the license and provide the corresponding source as required by
the AGPL-3.0.

## LiveActionV1 SPAN

The bundled LiveActionV1 SPAN model is Copyright 2025 jcj83429 and licensed under
the Apache License 2.0. Its notice and complete license are included at
`resources/licenses/LiveActionV1-NOTICE.txt` and
`resources/licenses/LiveActionV1-LICENSE.txt`.

## HFA2k and Nomos8k SPAN models

The bundled `hfa2k-span-2x` and `nomos8k-span-otf-medium` models are by
Helaman and are distributed under the Creative Commons Attribution 4.0
International license (`CC BY 4.0`). The included copies are packaged as NCNN
`.param`/`.bin` files for local inference. Attribution notices and the complete
license are included at `resources/licenses/HFA2k-NOTICE.txt`,
`resources/licenses/Nomos8k-NOTICE.txt`, and
`resources/licenses/CC-BY-4.0-LICENSE.txt`.

## Real-ESRGAN / AnimeVideoV3

The bundled `realesr-animevideov3` model originates from
[xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN), which is licensed
under the BSD 3-Clause license. Copyright and model rights remain with their
respective authors. The complete license is included at
`resources/licenses/Real-ESRGAN-LICENSE.txt`.

## Real HAT GAN

Real HAT GAN support is an external-runner integration only. No HAT executable or
model weights are included. Users who configure their own files are responsible
for complying with the license attached to those files.
