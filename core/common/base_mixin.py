# core/common/base_mixin.py
import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from random import choice
from urllib.parse import urlparse

import httpx
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .exceptions import SizeLimitExceeded
from .media import monitor_process_percentage
from .paths import get_xhs_card_path, get_xhs_image_path, get_xhs_video_path

TASK_NAME_PREFIX = "link-resolver-parse"


class BaseUtilsMixin:
    """基础工具与底层交互 Mixin"""

    # region 基础解析任务与缓存
    def _register_parse_task(
        self, kind: str, event: AstrMessageEvent | None = None
    ) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        message_id = None
        if event is not None:
            message_id = self._extract_reaction_message_id(event)
        tag = f"{kind}:{message_id or 'unknown'}"
        try:
            task.set_name(f"{TASK_NAME_PREFIX}:{tag}:{int(time.time() * 1000)}")
        except Exception:
            pass
        self._active_parse_tasks.add(task)
        task.add_done_callback(lambda t: self._active_parse_tasks.discard(t))

    def _cancel_previous_parse_tasks(self) -> None:
        cancelled: list[str] = []
        candidates: set[asyncio.Task] = set()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

        if loop:
            try:
                current_task = asyncio.current_task(loop=loop)
            except Exception:
                current_task = None
            try:
                tasks = asyncio.all_tasks(loop)
            except Exception:
                tasks = set()
            for task in tasks:
                if task is current_task:
                    continue
                name = task.get_name() if hasattr(task, "get_name") else ""
                if isinstance(name, str) and name.startswith(TASK_NAME_PREFIX):
                    candidates.add(task)
                    continue
                try:
                    qualname = getattr(task.get_coro(), "__qualname__", "")
                except Exception:
                    qualname = ""
                if any(
                    token in qualname
                    for token in (
                        "handle_xhs", "handle_weibo", "handle_douyin",
                        "handle_bili_video", "handle_twitter",
                        "_process_xhs", "_process_weibo", "_process_douyin",
                        "_process_bili_video", "_process_twitter",
                    )
                ):
                    candidates.add(task)

        for task in candidates:
            if task.done():
                continue
            try:
                task.cancel()
                name = task.get_name() if hasattr(task, "get_name") else ""
                if name:
                    cancelled.append(name)
            except Exception:
                continue

        if cancelled:
            logger.info("♻️ 插件重载，已中断旧解析任务 %d 个", len(cancelled))

    async def _auto_clean_expired_cache(self):
        """自动清理超过 7 天的本地图片/视频/卡片缓存"""
        ttl_seconds = 7 * 24 * 3600
        while True:
            try:
                def _scan_and_clean():
                    cleaned_count = 0
                    now = time.time()
                    dirs = [get_xhs_image_path(), get_xhs_video_path(), get_xhs_card_path()]
                    for d in dirs:
                        if d.exists():
                            for file in d.glob("*"):
                                if file.is_file():
                                    try:
                                        if now - file.stat().st_mtime > ttl_seconds:
                                            file.unlink(missing_ok=True)
                                            cleaned_count += 1
                                    except Exception:
                                        pass
                    return cleaned_count

                count = await asyncio.to_thread(_scan_and_clean)
                if count > 0:
                    logger.info("🧹 缓存清理完成: 已清除 %d 个过期缓存文件", count)
            except Exception as e:
                logger.warning("⚠️ 缓存清理出错: %s", str(e))
            await asyncio.sleep(12 * 3600)
    # endregion

    # region 常用底层格式化与校验
    def _has_json_component(self, event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for component in event.message_obj.message:
            if isinstance(component, dict):
                comp_type = component.get("type")
                if comp_type == "reply":
                    continue
                if comp_type and "json" in str(comp_type).lower():
                    return True
                continue
            if isinstance(component, Comp.Json):
                return True
            comp_type = getattr(component, "type", None)
            if comp_type and "json" in str(comp_type).lower():
                return True
        return False

    @staticmethod
    def _coerce_positive_int(value: object, default: int) -> int:
        if value is None or isinstance(value, bool):
            return default
        try:
            if isinstance(value, (int, float)):
                parsed = int(value)
                return parsed if parsed > 0 else default
            text = str(value).strip()
            if text.isdigit():
                parsed = int(text)
                return parsed if parsed > 0 else default
        except Exception:
            return default
        return default

    @staticmethod
    def _format_duration(duration_seconds: int | None) -> str | None:
        if not duration_seconds:
            return None
        minutes = int(duration_seconds) // 60
        seconds = int(duration_seconds) % 60
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def _hash_url(url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]

    @staticmethod
    def _guess_media_suffix(url: str, default: str) -> str:
        try:
            suffix = Path(urlparse(url).path).suffix
        except Exception:
            suffix = ""
        return suffix if (suffix and len(suffix) <= 5) else default

    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        return re.findall(r"https?://[^\s'\"<>]+", text) if text else []

    def _coerce_json_payload(self, json_component) -> dict | None:
        def unwrap(value, depth: int = 0) -> dict | None:
            if depth > 4 or value is None:
                return None
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
                try:
                    return unwrap(json.loads(value), depth + 1)
                except Exception:
                    return None
            if isinstance(value, dict):
                if any(key in value for key in ("meta", "prompt", "ver", "app", "view", "config")):
                    return value
                if "data" in value:
                    return unwrap(value["data"], depth + 1)
                return value
            if isinstance(value, list):
                for item in value:
                    payload = unwrap(item, depth + 1)
                    if payload:
                        return payload
            return None

        if hasattr(json_component, "data"):
            return unwrap(json_component.data)
        return unwrap(json_component)

    def extract_links_from_json(self, json_component) -> list[str]:
        links: list[str] = []
        try:
            json_data = self._coerce_json_payload(json_component)
            if not json_data:
                return links

            def search_json_for_links(obj):
                found: list[str] = []
                if isinstance(obj, dict):
                    for value in obj.values():
                        if isinstance(value, str):
                            found.extend(self._extract_urls_from_text(value))
                        elif isinstance(value, (dict, list)):
                            found.extend(search_json_for_links(value))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str):
                            found.extend(self._extract_urls_from_text(item))
                        elif isinstance(item, (dict, list)):
                            found.extend(search_json_for_links(item))
                return found

            links.extend(search_json_for_links(json_data))
            if isinstance(json_data, dict):
                meta = json_data.get("meta", {})
                detail = meta.get("detail_1", {}) if meta else {}
                if detail:
                    for key in ("qqdocurl", "url"):
                        value = detail.get(key, "")
                        if value:
                            links.extend(self._extract_urls_from_text(value))
        except Exception as exc:
            logger.warning("⚠️ 解析 JSON 消息组件失败: %s", str(exc))
        return links

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) == str(event.get_self_id())
        except Exception:
            return False

    async def _is_bot_muted(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if not group_id:
            return False
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False
        self_id = event.get_self_id()
        if not self_id:
            return False
        try:
            member_info = await bot.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=int(self_id), no_cache=True
            )
            shut_up_timestamp = member_info.get("shut_up_timestamp", 0)
            if shut_up_timestamp and shut_up_timestamp > time.time():
                logger.info("🔇 Bot 在群 %s 中被禁言，跳过处理", group_id)
                return True
        except Exception as exc:
            logger.debug("检测禁言状态失败: %s", str(exc))
        return False

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        if not group_id:
            return True
        gid = str(group_id)
        if self.group_filter_mode == "白名单":
            return gid in self.group_filter_list
        return gid not in self.group_filter_list

    def _extract_reaction_message_id(self, event: AstrMessageEvent) -> int | None:
        raw = getattr(event.message_obj, "raw_message", None)
        candidates = []
        if isinstance(raw, dict):
            candidates.append(raw.get("message_id"))
        elif raw is not None and hasattr(raw, "message_id"):
            candidates.append(getattr(raw, "message_id", None))
        candidates.append(getattr(event.message_obj, "message_id", None))
        for value in candidates:
            if value is None:
                continue
            try:
                mid = int(value)
                if mid > 0:
                    return mid
            except Exception:
                continue
        return None

    async def _send_reaction_emoji(self, event: AstrMessageEvent, source_tag: str) -> None:
        if not self.reaction_emoji_enabled or not self.reaction_emoji_list or not event.get_group_id():
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "set_msg_emoji_like"):
            return
        message_id = self._extract_reaction_message_id(event)
        if message_id is None:
            return
        emoji_ids = list(self.reaction_emoji_list) if self.reaction_emoji_strategy == "顺序循环" else [choice(self.reaction_emoji_list)]
        for emoji_id in emoji_ids:
            try:
                await bot.set_msg_emoji_like(
                    message_id=message_id, emoji_id=emoji_id, emoji_type=self.reaction_emoji_type, set=True
                )
            except Exception as exc:
                logger.warning("⚠️ 表情回应失败%s: %s", source_tag, str(exc))
            if len(emoji_ids) > 1:
                await asyncio.sleep(0.5)
    # endregion

    # region 下载与网络流工具
    async def _probe_stream_size(self, url: str, cookies: dict = None, headers: dict = None) -> int | None:
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers or {}, cookies=cookies or {}) as client:
                response = await client.head(url, follow_redirects=True)
                if response.status_code >= 400:
                    return None
                length = response.headers.get("Content-Length")
                if length:
                    return int(length)
                response = await client.get(url, headers={**(headers or {}), "Range": "bytes=0-0"})
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    return int(content_range.split("/")[-1])
        except Exception:
            return None
        return None

    async def _estimate_total_size_mb(self, video_url: str, audio_url: str | None, cookies: dict = None, headers: dict = None) -> float | None:
        total = 0
        unknown = False
        for url in (video_url, audio_url):
            if not url:
                continue
            size = await self._probe_stream_size(url, cookies=cookies, headers=headers)
            if size is None:
                unknown = True
                continue
            total += size
        if total == 0 and unknown:
            return None
        return total / 1024 / 1024

    async def _download_stream(self, url: str, output_path: Path, cookies: dict = None, max_bytes: int = None, headers: dict = None, retries: int = 3) -> int:
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        last_error = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=None, headers=headers or {}, cookies=cookies or {}) as client:
                    async with client.stream("GET", url, follow_redirects=True) as response:
                        response.raise_for_status()
                        content_length = response.headers.get("Content-Length")
                        if content_length and max_bytes and int(content_length) > max_bytes:
                            raise SizeLimitExceeded("超过大小限制")
                        bytes_written = 0
                        with open(temp_path, "wb") as file:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if not chunk:
                                    continue
                                bytes_written += len(chunk)
                                if max_bytes and bytes_written > max_bytes:
                                    raise SizeLimitExceeded("超过大小限制")
                                await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(temp_path.replace, output_path)
                return bytes_written
            except Exception as exc:
                last_error = exc
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                if attempt < retries - 1:
                    await asyncio.sleep(2**attempt)
        if last_error:
            raise last_error
        raise RuntimeError("下载失败")

    async def _merge_av(self, v_path: Path, a_path: Path, output_path: Path) -> None:
        cmd = ["ffmpeg", "-y", "-i", str(v_path), "-i", str(a_path), "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", str(output_path)]
        try:
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(stderr.decode().strip())
        finally:
            await asyncio.to_thread(v_path.unlink, missing_ok=True)
            await asyncio.to_thread(a_path.unlink, missing_ok=True)

    async def download_thumbnail(self, url: str, save_path: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    await asyncio.to_thread(save_path.write_bytes, response.content)
                    return True
        except Exception:
            pass
        return False

    async def calculate_md5(self, file_path: Path) -> str:
        def _sync_md5():
            hasher = hashlib.md5()
            with open(file_path, "rb") as file:
                while chunk := file.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        return await asyncio.to_thread(_sync_md5)

    async def cleanup_files(self, video_paths: list[Path], thumbnail_paths: list[Path]) -> None:
        for video_path in video_paths:
            await asyncio.to_thread(video_path.unlink, missing_ok=True)
        for thumb_path in thumbnail_paths:
            await asyncio.to_thread(thumb_path.unlink, missing_ok=True)

    def _get_merge_sender_uin(self, event: AstrMessageEvent) -> str:
        if self.merge_send_as_sender:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
        return str(event.get_self_id())

    async def _prepare_component_for_merge_send(self, component: Comp.BaseMessageComponent) -> Comp.BaseMessageComponent:
        if not isinstance(component, Comp.Video):
            return component
        file_ref = str(getattr(component, "file", "") or "").strip()
        if not file_ref or file_ref.startswith(("http://", "https://", "base64://")):
            return component
        try:
            callback_url = await component.register_to_file_service()
        except Exception:
            return component
        return Comp.Video.fromURL(callback_url, cover=getattr(component, "cover", ""), c=getattr(component, "c", 2))

    async def _send_notify(self, event: AstrMessageEvent, text: str) -> str | None:
        try:
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
            if call_action:
                params = {"message": text}
                group_id = getattr(event.message_obj, 'group_id', None) or getattr(event, 'group_id', None)
                if group_id:
                    params["group_id"] = int(group_id)
                else:
                    user_id = event.get_sender_id()
                    if user_id:
                        params["user_id"] = int(user_id)
                res = await call_action("send_msg", **params)
                if isinstance(res, dict) and "message_id" in res:
                    return res["message_id"]
        except Exception as e:
            logger.warning(f"[LinkResolver] 发送提示消息失败: {e}")
        return None

    async def _recall_notify(self, event: AstrMessageEvent, msg_id: str | None):
        if not msg_id:
            return
        try:
            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
            if call_action:
                await call_action("delete_msg", message_id=msg_id)
        except Exception as e:
            logger.debug(f"[LinkResolver] 撤回提示消息失败 (常见于私聊提示): {e}")

    async def _send_file_via_api(self, event: AstrMessageEvent, file_path: Path) -> bool:
        """通过 OneBot API 上传文件 (采用 Base64 数据流格式 100% 解决跨机器/异机部署 NapCat 识别绝对路径失败的问题)"""
        try:
            if not file_path.exists():
                logger.warning("⚠️ 文件不存在: %s", file_path)
                return False

            bot = getattr(event, "bot", None)
            call_action = getattr(bot, "call_action", None) or getattr(getattr(bot, "api", None), "call_action", None)
            file_name = file_path.name

            # 1. 核心改进：读取文件二进制流并转为 base64:// 格式，确保跨机器传输 100% 成功
            file_bytes = await asyncio.to_thread(file_path.read_bytes)
            b64_str = base64.b64encode(file_bytes).decode("ascii")
            base64_uri = f"base64://{b64_str}"

            group_id = getattr(event.message_obj, 'group_id', None) or getattr(event, 'group_id', None)

            if call_action:
                try:
                    if group_id:
                        await call_action('upload_group_file', group_id=int(group_id), file=base64_uri, name=file_name)
                    else:
                        user_id = event.get_sender_id()
                        if user_id:
                            await call_action('upload_private_file', user_id=int(user_id), file=base64_uri, name=file_name)
                    logger.info("📁 AVIF 文件已通过 Base64 API 成功发送: %s (%.1fKB)", file_name, len(file_bytes) / 1024)
                    return True
                except Exception as e_api:
                    logger.warning("⚠️ 通过 Base64 API 发送文件失败，改用 MessageChain 降级发送: %s", str(e_api))

            # 2. 降级方案：使用 MessageChain (从 astrbot.api.event 正确导入)
            await event.send(MessageChain([Comp.File.fromFileSystem(str(file_path.resolve()))]))
            logger.info("📁 AVIF 文件已通过 MessageChain 降级发送: %s", file_name)
            return True

        except Exception as e:
            logger.error("❌ 文件发送全线失败 (%s): %s", file_path.name, str(e))
            return False

    @property
    def heavy_task_lock(self) -> asyncio.Lock:
        if not hasattr(self, "_heavy_task_lock_obj"):
            self._heavy_task_lock_obj = asyncio.Lock()
        return self._heavy_task_lock_obj

    async def _monitor_process_percentage(self, proc: asyncio.subprocess.Process, stage_prefix: str) -> None:
        await monitor_process_percentage(proc, stage_prefix, self)

    async def _ai_upscale_platform_image(self, image_path, request_id, enable_flag, threshold_attr):
        if not getattr(self, enable_flag, True):
            return image_path
        threshold = getattr(self, threshold_attr, 1080)
        try:
            need_upscale, img_type, recommended_model = await self.upscaler.check_is_low_quality(image_path, threshold=threshold)
            if not need_upscale:
                return image_path

            if getattr(self, "current_task_info", None) is None:
                self.current_task_info = {
                    "title": image_path.stem, "user": "用户", "current_img": 1, "total_img": 1,
                    "stage": "🎨 AI 升图中", "percent": "0.0%", "start_time": time.time()
                }

            async with self.heavy_task_lock:
                upscaled_path = await self.upscaler.upscale_image(image_path, request_id, override_model=recommended_model)
                if upscaled_path != image_path and upscaled_path.exists():
                    return upscaled_path
        except Exception as e:
            logger.warning("⚠️ AI 升图处理异常: %s", str(e))
        return image_path

    async def _ffmpeg_compress_av1(self, input_path: Path, request_id: str) -> Path | None:
        return await self.encoder.compress_avif(input_path, request_id)

    async def _generate_jpg_preview(self, image_path: Path, request_id: str) -> Path | None:
        return await self.encoder.generate_jpg_preview(image_path, request_id)

    async def _convert_to_avif_with_preview(self, image_path: Path, request_id: str) -> tuple[Path | None, Path | None]:
        return await self.encoder.convert_to_avif_with_preview(image_path, request_id)
    # endregion