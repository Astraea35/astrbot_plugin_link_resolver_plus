from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.api.message_components import Image, Reply
from core.common.base_mixin import BaseUtilsMixin
from core.common.image_tool_mixin import ImageToolMixin


@pytest.mark.asyncio
async def test_reply_images_are_available_without_onebot_calls(tmp_path):
    path = tmp_path / "input.png"
    path.write_bytes(b"image")
    image = Image.fromFileSystem(str(path))
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=[Reply(id="1", chain=[image])]),
        bot=None,
    )
    tool = ImageToolMixin()
    assert await tool._extract_image_urls_for_tool(event)


@pytest.mark.asyncio
async def test_telegram_files_use_the_platform_transport(tmp_path):
    file = tmp_path / "result.avif"
    file.write_bytes(b"result")
    event = SimpleNamespace(get_platform_name=lambda: "telegram", send=AsyncMock())
    tool = BaseUtilsMixin()
    assert await tool._send_file_via_api(event, file)
    chain = event.send.await_args.args[0]
    assert chain.chain[0].name == "result.avif"


@pytest.mark.asyncio
async def test_tool_download_uses_nas_proxy_without_environment_fallback(monkeypatch, tmp_path):
    import httpx

    response = SimpleNamespace(content=b"image", raise_for_status=lambda: None)
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    factory = Mock(return_value=manager)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    tool = ImageToolMixin()
    tool.download_proxy = "http://192.168.1.34:7890"
    tool._get_config_value = lambda key, default: default
    tool._image_tool_cache_dir = lambda: tmp_path
    path = await tool._download_tool_image("https://example.com/image.png")
    assert path.read_bytes() == b"image"
    assert factory.call_args.kwargs["proxy"] == tool.download_proxy
    assert factory.call_args.kwargs["trust_env"] is False
