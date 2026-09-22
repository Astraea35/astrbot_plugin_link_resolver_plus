# core/common/media/remote_client.py
import asyncio
from pathlib import Path
import httpx
from astrbot.api import logger


class RemoteWorkerClient:
    """与 PC 端远程算力节点 (FastAPI Worker) 交互的 HTTP 客户端。"""

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance

    @property
    def is_enabled(self) -> bool:
        return bool(getattr(self.plugin, "remote_worker_enabled", False))

    @property
    def worker_url(self) -> str:
        url = str(getattr(self.plugin, "remote_worker_url", "http://192.168.1.100:8899")).strip()
        return url.rstrip("/")

    @property
    def token(self) -> str:
        return str(getattr(self.plugin, "remote_worker_token", "") or "").strip()

    @property
    def timeout_sec(self) -> float:
        try:
            return float(getattr(self.plugin, "remote_worker_timeout", 180.0))
        except (TypeError, ValueError):
            return 180.0

    @property
    def fallback_policy(self) -> str:
        return str(getattr(self.plugin, "remote_worker_fallback_policy", "send_original")).strip()

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-API-Key"] = self.token
        return headers

    async def check_health(self) -> dict | None:
        """快速健康探测（3 秒超时）"""
        if not self.is_enabled:
            return None
        url = f"{self.worker_url}/health"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(url, headers=self._headers())
                if res.status_code == 200:
                    return res.json()
        except Exception as exc:
            logger.debug("远程算力节点健康检查失败 (%s): %s", url, exc)
        return None

    async def upscale_image(
        self,
        input_path: Path,
        output_path: Path,
        model_name: str,
        scale: int = 4,
        enable_taa: bool = False,
        passes: int = 1,
    ) -> bool:
        """将图片上传至 PC 算力节点进行超分辨率处理，流式接收结果并落盘。"""
        url = f"{self.worker_url}/api/upscale"
        timeout = httpx.Timeout(self.timeout_sec, connect=10.0)

        data = {
            "model": model_name,
            "scale": str(scale),
            "enable_taa": str(bool(enable_taa)).lower(),
            "passes": str(max(1, int(passes))),
        }

        logger.info(
            "🚀 [远程算力] 正在将 %s 提交至 PC 节点执行 AI 升图 (模型: %s, 倍率: %sx, 轮次: %s)",
            input_path.name,
            model_name,
            scale,
            passes,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                with input_path.open("rb") as f:
                    files = {"file": (input_path.name, f, "image/png")}
                    async with client.stream(
                        "POST", url, data=data, files=files, headers=self._headers()
                    ) as response:
                        if response.status_code != 200:
                            err_body = (await response.aread()).decode("utf-8", "ignore")
                            logger.error("❌ 远程算力节点升图返回错误 (HTTP %s): %s", response.status_code, err_body)
                            if self.fallback_policy == "raise_error":
                                raise RuntimeError(f"Remote worker upscale failed (HTTP {response.status_code}): {err_body}")
                            return False

                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with output_path.open("wb") as out_f:
                            async for chunk in response.aiter_bytes(64 * 1024):
                                out_f.write(chunk)

            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info("✅ [远程算力] AI 升图完成: %s (%.1fKB)", output_path.name, output_path.stat().st_size / 1024)
                return True
            return False

        except Exception as exc:
            logger.error("❌ 调用远程算力升图发生异常: %s", exc)
            output_path.unlink(missing_ok=True)
            if self.fallback_policy == "raise_error":
                raise
            return False

    async def encode_image(
        self,
        input_path: Path,
        output_path: Path,
        fmt: str = "AVIF",
        cpu_used: int = 1,
        crf: int = 18,
        distance: float = 1.0,
        effort: int = 9,
    ) -> bool:
        """将图片上传至 PC 算力节点进行 AVIF / JXL 转码。"""
        url = f"{self.worker_url}/api/encode"
        timeout = httpx.Timeout(self.timeout_sec, connect=10.0)

        data = {
            "format": fmt,
            "cpu_used": str(cpu_used),
            "crf": str(crf),
            "distance": str(distance),
            "effort": str(effort),
        }

        logger.info("🚀 [远程算力] 正在将 %s 提交至 PC 节点执行 %s 压缩转码", input_path.name, fmt)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                with input_path.open("rb") as f:
                    files = {"file": (input_path.name, f, "image/png")}
                    async with client.stream(
                        "POST", url, data=data, files=files, headers=self._headers()
                    ) as response:
                        if response.status_code != 200:
                            err_body = (await response.aread()).decode("utf-8", "ignore")
                            logger.error("❌ 远程算力节点转码返回错误 (HTTP %s): %s", response.status_code, err_body)
                            if self.fallback_policy == "raise_error":
                                raise RuntimeError(f"Remote worker encode failed (HTTP {response.status_code}): {err_body}")
                            return False

                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with output_path.open("wb") as out_f:
                            async for chunk in response.aiter_bytes(64 * 1024):
                                out_f.write(chunk)

            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info("✅ [远程算力] %s 转码完成: %s (%.1fKB)", fmt, output_path.name, output_path.stat().st_size / 1024)
                return True
            return False

        except Exception as exc:
            logger.error("❌ 调用远程算力转码发生异常: %s", exc)
            output_path.unlink(missing_ok=True)
            if self.fallback_policy == "raise_error":
                raise
            return False
