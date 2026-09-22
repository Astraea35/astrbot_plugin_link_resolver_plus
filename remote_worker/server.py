# server.py
import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
import uvicorn

import sys

from config import load_config
from runner import run_encode, run_upscale

config = load_config()
app = FastAPI(title="Link Resolver Remote Worker", version="1.0.0")


def apply_affinity_and_priority():
    if sys.platform != "win32":
        return
    affinity_hex = config.get("cpu_affinity_mask", "0xFF000")
    priority = str(config.get("process_priority", "IDLE")).upper()
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_ulonglong]
        k.SetProcessAffinityMask.restype = wintypes.BOOL
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetPriorityClass.restype = wintypes.BOOL

        p = k.GetCurrentProcess()
        if affinity_hex:
            mask = int(affinity_hex, 16) if isinstance(affinity_hex, str) else int(affinity_hex)
            if k.SetProcessAffinityMask(p, mask):
                print(f"[CPU亲和性] 已锁定至 E核 (掩码: {affinity_hex})，彻底隔离大核(P核)")

        prio_map = {"IDLE": 0x40, "BELOW_NORMAL": 0x4000, "NORMAL": 0x20}
        prio_val = prio_map.get(priority, 0x40)
        if k.SetPriorityClass(p, prio_val):
            print(f"[进程优先级] 已设置为 {priority}，优先让出算力给 MC 服务器")
    except Exception as exc:
        print(f"[警告] 设置 CPU 亲和性或优先级失败: {exc}")


apply_affinity_and_priority()

# 严格保护 MC 服务器的并发锁
max_concurrency = max(1, int(config.get("max_concurrency", 1)))
task_semaphore = asyncio.Semaphore(max_concurrency)


def verify_token(token_header: Optional[str] = None):
    required_token = str(config.get("token", "")).strip()
    if not required_token:
        return  # 未配置 token 则内网免密
    if not token_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token (X-API-Key or Authorization header required)",
        )
    # 兼容 "Bearer <token>" 和纯字符串
    provided = token_header.replace("Bearer ", "").strip()
    if provided != required_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )


def cleanup_files(*paths: Path):
    for p in paths:
        try:
            if p and p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 放行健康检查
    if request.url.path == "/health":
        return await call_next(request)

    required_token = str(config.get("token", "")).strip()
    if required_token:
        auth_header = request.headers.get("Authorization") or request.headers.get("X-API-Key")
        try:
            verify_token(auth_header)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.get("/health")
async def health():
    upscayl_bin = config.get("upscayl_bin_path") or ""
    ffmpeg_bin = config.get("ffmpeg_bin_path") or "ffmpeg"
    has_upscayl = bool(upscayl_bin and Path(upscayl_bin).is_file())
    has_ffmpeg = bool(shutil.which(ffmpeg_bin) or Path(ffmpeg_bin).is_file())
    return {
        "status": "ok",
        "service": "link_resolver_remote_worker",
        "concurrency_limit": max_concurrency,
        "e_core_only": bool(config.get("e_core_only", True)),
        "cpu_affinity_mask": config.get("cpu_affinity_mask", "0xFF000"),
        "upscayl_ready": has_upscayl,
        "ffmpeg_ready": has_ffmpeg,
    }


@app.post("/api/upscale")
async def api_upscale(
    file: UploadFile = File(...),
    model: str = Form("digital-art-4x"),
    scale: int = Form(4),
    enable_taa: bool = Form(False),
    passes: int = Form(1),
):
    temp_dir = Path(config.get("temp_dir") or tempfile.gettempdir()) / "remote_worker"
    temp_dir.mkdir(parents=True, exist_ok=True)

    uid = uuid.uuid4().hex
    in_ext = Path(file.filename or "temp.png").suffix or ".png"
    input_path = temp_dir / f"up_in_{uid}{in_ext}"
    output_path = temp_dir / f"up_out_{uid}.png"

    # 流式写入上传的图片文件
    with input_path.open("wb") as f:
        while chunk := await file.read(64 * 1024):
            f.write(chunk)

    async with task_semaphore:
        success = await run_upscale(
            input_path=input_path,
            output_path=output_path,
            model_name=model,
            scale=scale,
            enable_taa=enable_taa,
            passes=passes,
            config=config,
        )

    if not success or not output_path.exists():
        cleanup_files(input_path, output_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upscale processing failed for model {model}",
        )

    return FileResponse(
        path=output_path,
        media_type="image/png",
        filename=output_path.name,
        background=BackgroundTask(cleanup_files, input_path, output_path),
    )


@app.post("/api/encode")
async def api_encode(
    file: UploadFile = File(...),
    format: str = Form("AVIF"),
    cpu_used: int = Form(1),
    crf: int = Form(18),
    distance: float = Form(1.0),
    effort: int = Form(9),
):
    temp_dir = Path(config.get("temp_dir") or tempfile.gettempdir()) / "remote_worker"
    temp_dir.mkdir(parents=True, exist_ok=True)

    uid = uuid.uuid4().hex
    in_ext = Path(file.filename or "temp.png").suffix or ".png"
    input_path = temp_dir / f"enc_in_{uid}{in_ext}"

    fmt_upper = str(format).upper()
    if fmt_upper.startswith("JXL"):
        out_ext = ".jxl"
        media_type = "image/jxl"
    elif fmt_upper.startswith("JPG") or fmt_upper.startswith("JPEG"):
        out_ext = ".jpg"
        media_type = "image/jpeg"
    else:
        out_ext = ".avif"
        media_type = "image/avif"
    output_path = temp_dir / f"enc_out_{uid}{out_ext}"

    with input_path.open("wb") as f:
        while chunk := await file.read(64 * 1024):
            f.write(chunk)

    async with task_semaphore:
        success = await run_encode(
            input_path=input_path,
            output_path=output_path,
            fmt=fmt_upper,
            cpu_used=cpu_used,
            crf=crf,
            distance=distance,
            effort=effort,
            config=config,
        )

    if not success or not output_path.exists():
        cleanup_files(input_path, output_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Encode processing failed for format {format}",
        )

    return FileResponse(
        path=output_path,
        media_type=media_type,
        filename=output_path.name,
        background=BackgroundTask(cleanup_files, input_path, output_path),
    )


if __name__ == "__main__":
    host = str(config.get("host", "0.0.0.0"))
    port = int(config.get("port", 8899))
    print(f"==================================================")
    print(f"  Link Resolver 远程算力节点正在启动...")
    print(f"  监听地址: http://{host}:{port}")
    print(f"  保护 MC 服务并发限制: {max_concurrency}")
    print(f"  CPU 调度模式: 严格锁定 E核 ({config.get('cpu_affinity_mask', '0xFF000')}) + {config.get('process_priority', 'IDLE')} 优先级")
    print(f"  鉴权状态: {'已启用 (Token 保护)' if config.get('token') else '未启用 (内网免密模式)'}")
    print(f"==================================================")
    uvicorn.run("server:app", host=host, port=port, log_level="info")
