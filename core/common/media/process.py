# core/common/media/process.py
import asyncio
import re
import time
from astrbot.api import logger


async def monitor_process_percentage(
    proc: asyncio.subprocess.Process,
    stage_prefix: str,
    plugin_instance,
    total_duration_sec: float | None = None
) -> None:
    """实时捕获子进程 (FFmpeg / Upscayl) 进度并隔绝日志展示：
    - AI 生图 (带百分比): 输出 % 并在末尾带上已耗时，如 `🎨 AI 升图中 [1] 52.0% (已耗时 21s)`
    - FFmpeg (单图无百分比): 每 2 秒输出纯耗时打点，如 `🗜️ FFmpeg AV1 压缩中 [1] 已耗时 4s`
    """
    start_time = time.time()
    percent_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    ffmpeg_time_pattern = re.compile(r"(?:time|out_time)=(\d+):(\d+):(\d+(?:\.\d+)?)")

    stream = proc.stderr or proc.stdout
    if not stream:
        await proc.wait()
        return

    interval = max(1, min(100, int(getattr(plugin_instance, "progress_report_interval", 1))))
    last_logged_pct = -999.0
    last_heartbeat_sec = 0
    has_percentage = False  # 是否检测到百分比信号

    buffer = ""

    while True:
        try:
            # 1 秒超时读流，用于捕获输出或触发无百分比时的秒级心跳
            chunk_bytes = await asyncio.wait_for(stream.read(256), timeout=1.0)
            if not chunk_bytes:
                break
            buffer += chunk_bytes.decode('utf-8', errors='ignore')

            # 拆分 \r 与 \n 换行符
            while '\r' in buffer or '\n' in buffer:
                pos_r = buffer.find('\r')
                pos_n = buffer.find('\n')
                if pos_r != -1 and (pos_n == -1 or pos_r < pos_n):
                    text = buffer[:pos_r]
                    buffer = buffer[pos_r + 1:]
                else:
                    text = buffer[:pos_n]
                    buffer = buffer[pos_n + 1:]

                if not text.strip():
                    continue

                pct_val = None

                # 1. 尝试匹配百分比 (如 Upscayl)
                match_pct = percent_pattern.search(text)
                if match_pct:
                    try:
                        pct_val = float(match_pct.group(1))
                    except ValueError:
                        pass

                # 2. 尝试匹配视频 FFmpeg time
                if pct_val is None:
                    match_time = ffmpeg_time_pattern.search(text)
                    if match_time and total_duration_sec and total_duration_sec > 0:
                        try:
                            h, m, s = float(match_time.group(1)), float(match_time.group(2)), float(match_time.group(3))
                            curr_sec = h * 3600 + m * 60 + s
                            pct_val = min(100.0, (curr_sec / total_duration_sec) * 100.0)
                        except ValueError:
                            pass

                task_info = getattr(plugin_instance, "current_task_info", None)

                # 🚀 分支 A：带百分比模式 (AI 生图 / 视频转码)
                if pct_val is not None:
                    has_percentage = True  # 标记当前任务有百分比，完全屏蔽纯秒数打点
                    elapsed_sec = int(time.time() - start_time)
                    if task_info is not None:
                        task_info["stage"] = stage_prefix
                        task_info["percent"] = f"{pct_val:.1f}% ({elapsed_sec}s)"
                        
                        if abs(pct_val - last_logged_pct) >= interval or pct_val == 100.0 or last_logged_pct < 0:
                            logger.info("%s [%s] %.1f%% (已耗时 %ds)", stage_prefix, task_info.get("current_img", "?"), pct_val, elapsed_sec)
                            last_logged_pct = pct_val

        except asyncio.TimeoutError:
            # 🚀 分支 B：纯时间心跳 (仅在没有百分比的 FFmpeg 单图压制时触发)
            if not has_percentage:
                elapsed_sec = int(time.time() - start_time)
                if elapsed_sec >= last_heartbeat_sec + 2 and proc.returncode is None:
                    last_heartbeat_sec = elapsed_sec
                    task_info = getattr(plugin_instance, "current_task_info", None)
                    if task_info is not None:
                        task_info["stage"] = stage_prefix
                        task_info["percent"] = f"已处理 {elapsed_sec}s"
                        logger.info("%s [%s] 已耗时 %ds", stage_prefix, task_info.get("current_img", "?"), elapsed_sec)

    await proc.wait()
    if proc.returncode != 0:
        logger.warning("⚠️ %s 执行异常 (returncode=%s)", stage_prefix, proc.returncode)