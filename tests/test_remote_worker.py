import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 确保 plugin_root 在 sys.path 中
plugin_root = Path(__file__).resolve().parents[1]
if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))

# Stub astrbot 依赖
astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = MagicMock()
astrbot_module.api = astrbot_api_module
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

# Stub httpx 如果未安装
if "httpx" not in sys.modules:
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        httpx_stub = types.ModuleType("httpx")
        class Timeout:
            def __init__(self, *args, **kwargs):
                pass
        class AsyncClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        httpx_stub.Timeout = Timeout
        httpx_stub.AsyncClient = AsyncClient
        sys.modules["httpx"] = httpx_stub

from core.common.media.remote_client import RemoteWorkerClient
from core.common.media.upscaler import UpscaylUpscaler
from core.common.media.encoder import MediaEncoder


class TestRemoteWorkerIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_path = Path(self.temp_dir.name)
        self.dummy_img = self.work_path / "test.png"
        self.dummy_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        self.plugin = MagicMock()
        self.plugin.remote_worker = None
        self.plugin.remote_worker_enabled = True
        self.plugin.remote_worker_url = "http://127.0.0.1:8899"
        self.plugin.remote_worker_token = "secret123"
        self.plugin.remote_worker_timeout = 30
        self.plugin.remote_worker_fallback_policy = "send_original"
        self.plugin.upscayl_model_name = "digital-art-4x"
        self.plugin.preserve_image_metadata = False

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_remote_client_properties_and_headers(self):
        client = RemoteWorkerClient(self.plugin)
        self.assertTrue(client.is_enabled)
        self.assertEqual(client.worker_url, "http://127.0.0.1:8899")
        self.assertEqual(client.token, "secret123")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Bearer secret123")
        self.assertEqual(headers["X-API-Key"], "secret123")

    async def test_remote_upscale_success(self):
        client = RemoteWorkerClient(self.plugin)
        output_file = self.work_path / "out_upscaled.png"

        class MockResponse:
            status_code = 200
            async def aiter_bytes(self, chunk_size=64*1024):
                yield b"\x89PNG\r\n\x1a\nfake_upscaled_data"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, *args, **kwargs):
                return MockResponse()

        with patch("core.common.media.remote_client.httpx.AsyncClient", new=MockClient):
            success = await client.upscale_image(
                self.dummy_img,
                output_file,
                model_name="digital-art-4x",
                scale=4,
                enable_taa=True,
                passes=1,
            )
            self.assertTrue(success)
            self.assertTrue(output_file.exists())
            self.assertIn(b"fake_upscaled_data", output_file.read_bytes())

    async def test_remote_upscale_fallback_send_original(self):
        self.plugin.remote_worker_fallback_policy = "send_original"
        upscaler = UpscaylUpscaler(self.plugin)

        # 模拟远程客户端失败返回 False
        upscaler.remote_client.upscale_image = AsyncMock(return_value=False)

        res_path = await upscaler.upscale_image(self.dummy_img, "req-1")
        # 降级返回原图路径
        self.assertEqual(res_path, self.dummy_img)

    async def test_remote_upscale_fallback_raise_error(self):
        self.plugin.remote_worker_fallback_policy = "raise_error"
        upscaler = UpscaylUpscaler(self.plugin)

        upscaler.remote_client.upscale_image = AsyncMock(return_value=False)

        with self.assertRaises(RuntimeError):
            await upscaler.upscale_image(self.dummy_img, "req-1")

    async def test_single_machine_mode_bypasses_remote(self):
        # 关闭远程节点，应完全走本地代码
        self.plugin.remote_worker_enabled = False
        upscaler = UpscaylUpscaler(self.plugin)
        upscaler.remote_client.upscale_image = AsyncMock()

        # 模拟本地 _run_model
        with patch.object(upscaler, "_run_model", new=AsyncMock(return_value=True)):
            res_path = await upscaler.upscale_image(self.dummy_img, "req-2", scale=2)
            # remote_client.upscale_image 绝对不应该被调用
            upscaler.remote_client.upscale_image.assert_not_called()
            self.assertNotEqual(res_path, self.dummy_img)

    async def test_remote_encoder_success(self):
        client = RemoteWorkerClient(self.plugin)
        output_file = self.work_path / "out_encoded.avif"

        class MockResponse:
            status_code = 200
            async def aiter_bytes(self, chunk_size=64*1024):
                yield b"fake_avif_data"
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, *args, **kwargs):
                return MockResponse()

        with patch("core.common.media.remote_client.httpx.AsyncClient", new=MockClient):
            success = await client.encode_image(
                self.dummy_img,
                output_file,
                fmt="AVIF",
            )
            self.assertTrue(success)
            self.assertTrue(output_file.exists())
            self.assertIn(b"fake_avif_data", output_file.read_bytes())


if __name__ == "__main__":
    unittest.main()
