import sys
import types
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
