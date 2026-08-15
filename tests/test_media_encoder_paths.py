import sys
import types
import tempfile
import unittest
from pathlib import Path

media_package = types.ModuleType("core.common.media")
media_package.__path__ = [str(Path(__file__).resolve().parents[1] / "core" / "common" / "media")]
sys.modules.setdefault("core.common.media", media_package)

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace()
astrbot_module.api = astrbot_api_module
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

metadata_module = types.ModuleType("core.common.media.metadata")
metadata_module.ImageMetadataStore = type("ImageMetadataStore", (), {})
sys.modules.setdefault("core.common.media.metadata", metadata_module)

process_module = types.ModuleType("core.common.media.process")
process_module.monitor_process_percentage = None
sys.modules.setdefault("core.common.media.process", process_module)

from core.common.media.encoder import MediaEncoder


class MediaEncoderPathTests(unittest.TestCase):
    def test_defaults_to_avif_compression(self):
        encoder = MediaEncoder(types.SimpleNamespace())

        options = encoder._image_compression_options(Path("C:/media/photo.jpg"))

        self.assertEqual(options["format"], "AVIF")
        self.assertEqual(options["suffix"], "_av1.avif")
        self.assertIn("libaom-av1", options["codec_args"])

    def test_uses_configured_jxl_distance_for_non_png_images(self):
        encoder = MediaEncoder(
            types.SimpleNamespace(image_compress_format="JXL", jxl_distance="0.8")
        )

        options = encoder._image_compression_options(Path("C:/media/photo.jpg"))

        self.assertEqual(options["format"], "JXL")
        self.assertEqual(options["suffix"], "_jxl_d0.8.jxl")
        self.assertEqual(options["parameters"], {"effort": 9, "distance": 0.8})
        self.assertEqual(options["codec_args"][-1], "0.8")

    def test_jxl_forces_lossless_distance_for_png_images(self):
        encoder = MediaEncoder(
            types.SimpleNamespace(image_compress_format="JXL", jxl_distance="2.0")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "photo.jpg"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nPNG data")
            options = encoder._image_compression_options(image_path)

        self.assertEqual(options["suffix"], "_jxl_lossless.jxl")
        self.assertEqual(options["parameters"], {"effort": 9, "distance": 0.0})
        self.assertEqual(options["codec_args"][-1], "0")

    def test_jxl_does_not_treat_a_non_png_as_lossless_by_extension(self):
        encoder = MediaEncoder(
            types.SimpleNamespace(image_compress_format="JXL", jxl_distance="2.0")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "raw.png"
            image_path.write_bytes(b"\xff\xd8\xffJPEG data")
            options = encoder._image_compression_options(image_path)

        self.assertEqual(options["suffix"], "_jxl_d2.0.jxl")
        self.assertEqual(options["codec_args"][-1], "2.0")

    def test_invalid_jxl_distance_falls_back_to_visual_lossless(self):
        encoder = MediaEncoder(
            types.SimpleNamespace(image_compress_format="JXL", jxl_distance="invalid")
        )

        options = encoder._image_compression_options(Path("C:/media/photo.webp"))

        self.assertEqual(options["suffix"], "_jxl_d1.0.jxl")
        self.assertEqual(options["codec_args"][-1], "1.0")

    def test_preserves_readable_name_when_path_is_short(self):
        input_path = Path("C:/media/photo.png")

        output_path = MediaEncoder._build_output_path(input_path, "_av1.avif")

        self.assertEqual(output_path, Path("C:/media/photo_av1.avif"))

    def test_shortens_long_output_name_and_keeps_suffix(self):
        input_path = Path("C:/media") / f"{'x' * 240}.png"

        output_path = MediaEncoder._build_output_path(input_path, "_preview.jpg")

        self.assertLessEqual(
            len(str(output_path)) + MediaEncoder._METADATA_SIDECAR_LENGTH,
            MediaEncoder._MAX_OUTPUT_PATH_LENGTH,
        )
        self.assertEqual(output_path.parent, input_path.parent)
        self.assertTrue(output_path.name.endswith("_preview.jpg"))
        self.assertNotEqual(output_path.name, f"{input_path.stem}_preview.jpg")

    def test_uses_temp_directory_when_parent_path_is_too_deep(self):
        input_path = Path("C:/") / ("nested/" * 40) / "photo.png"

        output_path = MediaEncoder._build_output_path(input_path, "_av1.avif")

        self.assertEqual(output_path.parent.name, "astrbot-link-resolver-plus")
        self.assertTrue(output_path.name.endswith("_av1.avif"))
