# core/common/commands_mixin.py
import asyncio
import re
import time
from pathlib import Path
import httpx
from bilibili_api import Credential as BiliCredential
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .paths import get_bili_cookies_file


class CommandsMixin:
    """用户指令过滤器 Mixin (@filter.command)"""

    # region 进度查询命令
    @filter.command("小红书进度")
    async def cmd_query_xhs_progress_1(self, event: AstrMessageEvent):
        async for res in self._do_query_xhs_progress(event):
            yield res

    @filter.command("解析进度")
    async def cmd_query_xhs_progress_2(self, event: AstrMessageEvent):
        async for res in self._do_query_xhs_progress(event):
            yield res

    @filter.command("生图进度")
    async def cmd_query_xhs_progress_3(self, event: AstrMessageEvent):
        async for res in self._do_query_xhs_progress(event):
            yield res

    async def _do_query_xhs_progress(self, event: AstrMessageEvent):
        task_info = getattr(self, "current_task_info", None)
        if not task_info:
            yield event.plain_result("🟢 当前没有正在执行的解析、生图或转码任务。")
            return

        title = task_info.get("title", "媒体图集")
        user = task_info.get("user", "未知用户")
        curr = task_info.get("current_img", 0)
        total = task_info.get("total_img", 0)
        stage = task_info.get("stage", "处理中")
        percent = task_info.get("percent", "0.0%")
        elapsed = int(time.time() - task_info.get("start_time", time.time()))

        msg = (
            f"⚙️ 媒体处理进度清单：\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📌 任务标题：{title}\n"
            f"👤 发起用户：{user}\n"
            f"🖼️ 处理进度：[图片 {curr}/{total}]\n"
            f"🔄 当前阶段：{stage}\n"
            f"📊 阶段进度：{percent}\n"
            f"⏱️ 总计耗时：{elapsed} 秒"
        )
        yield event.plain_result(msg)
    # endregion

    # region B站扫码登录
    @filter.command("扫码登录B站")
    async def cmd_qrcode_login_bilibili(self, event: AstrMessageEvent):
        try:
            yield event.plain_result("🔄 正在生成 B站 扫码登录二维码，请稍候...")
            qr_login = QrCodeLogin()
            await qr_login.generate_qrcode()
            
            if not qr_login.has_qrcode():
                yield event.plain_result("❌ 生成二维码失败，请稍后重试")
                return
            
            qr_pic = qr_login.get_qrcode_picture()
            qr_url = qr_pic.url
            logger.info("📱 B站扫码登录二维码URL: %s", qr_url)
            
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(qr_url, headers={"User-Agent": "Mozilla/5.0"})
                    qr_path = Path(get_bili_cookies_file().parent) / "qrcode_login.png"
                    qr_path.write_bytes(resp.content)
                    yield event.chain_result([
                        Comp.Image.fromFileSystem(str(qr_path.resolve())),
                        Comp.Plain("📱 请使用 B站 App 扫描上方二维码登录\n⏱️ 二维码有效期为 3 分钟\n✅ 扫码并确认后自动保存 Cookie")
                    ])
            except Exception as e:
                yield event.plain_result(f"📱 请手动打开链接扫码登录: {qr_url}\n(下载二维码图片失败: {e})")
            
            poll_count = 0
            while poll_count < 90:
                await asyncio.sleep(2)
                poll_count += 1
                try:
                    status = await qr_login.check_state()
                    if status == QrCodeLoginEvents.DONE:
                        credential = qr_login.get_credential()
                        cookies_file = get_bili_cookies_file()
                        cookies_file.parent.mkdir(parents=True, exist_ok=True)
                        expire_ts = int(time.time()) + 180 * 86400
                        cookie_lines = [
                            "# Netscape HTTP Cookie File",
                            f".bilibili.com\tTRUE\t/\tFALSE\t{expire_ts}\tSESSDATA\t{credential.sessdata}",
                            f".bilibili.com\tTRUE\t/\tFALSE\t{expire_ts}\tbili_jct\t{credential.bili_jct}",
                            f".bilibili.com\tTRUE\t/\tFALSE\t{expire_ts}\tDedeUserID\t{credential.dedeuserid}",
                        ]
                        cookies_file.write_text("\n".join(cookie_lines), encoding="utf-8")
                        yield event.plain_result(
                            f"✅ B站扫码登录成功！\n"
                            f"📄 Cookie 已保存至: {cookies_file.name}\n"
                            f"✨ Cookie 已实时生效，无需重启"
                        )
                        try:
                            qr_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return
                    elif status == QrCodeLoginEvents.TIMEOUT:
                        yield event.plain_result("⏰ 二维码已过期，请重新发送「扫码登录B站」命令")
                        return
                except Exception as e:
                    logger.warning("⚠️ 轮询 B站 扫码状态异常: %s", str(e))
            yield event.plain_result("⏰ 扫码超时（3分钟），请重新发送「扫码登录B站」命令")
        except Exception as e:
            logger.error("❌ B站扫码登录失败: %s", str(e))
            yield event.plain_result(f"❌ B站扫码登录失败: {str(e)[:100]}")
    # endregion

    # region B站手动下载
    @filter.command("下载B站")
    async def cmd_download_bili(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        for prefix in ("下载B站", "下载b站", "下载bilibili", "下载Bilibili"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
                break
        from ..bilibili.handler import BILI_MESSAGE_PATTERN, BilibiliMixin
        if not text or not re.search(BILI_MESSAGE_PATTERN, text):
            yield event.plain_result("❌ 请提供 B站 视频链接，例如：下载B站 https://www.bilibili.com/video/BV1xx411c7mD")
            return
        if not getattr(self, "bili_enabled", True):
            yield event.plain_result("❌ B站 平台未启用，请先在插件配置中勾选 B站")
            return
        notify_id = await self._send_notify(event, "⏳ 正在手动下载 B站 视频，请稍候...")
        try:
            self._register_parse_task("bili-manual", event)
            await BilibiliMixin.handle_bili_video(self, event)
        finally:
            await self._recall_notify(event, notify_id)
    # endregion