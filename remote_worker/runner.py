# runner.py
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from PIL import Image

MODEL_MAPPING = {
    "自动 (CV特征识别)": "auto",
    "数字艺术 (digital-art-4x)": "digital-art-4x",
    "二次元 (digital-art-4x)": "digital-art-4x",
    "高保真 (high-fidelity-4x)": "high-fidelity-4x",
    "Remacri (remacri-4x)": "remacri-4x",
    "超混合平衡 (ultramix-balanced-4x)": "ultramix-balanced-4x",
    "超锐化 (ultrasharp-4x)": "ultrasharp-4x",
    "轻量 (upscayl-lite-4x)": "upscayl-lite-4x",
    "标准 (upscayl-standard-4x)": "upscayl-standard-4x",
    "极速动漫 (realesr-animevideov3)": "realesr-animevideov3",
    "照片自然 2x (liveaction-v1-span-2x)": "liveaction-v1-span-2x",
    "照片自然 4x (nomos8k-span-otf-medium)": "nomos8k-span-otf-medium",
    "动漫自然 2x (hfa2k-span-2x)": "hfa2k-span-2x",
    "动漫高质量 (animejanai-v3.1-balanced)": "animejanai-v3.1-balanced",
    "动漫高质量锐利 (animejanai-v3.1-sharp)": "animejanai-v3.1-sharp",
    "自然照片 2x (liveaction-v1-span-2x)": "liveaction-v1-span-2x",
    "自然照片 4x (nomos8k-span-otf-medium)": "nomos8k-span-otf-medium",
}

MODEL_BACKENDS = {
    "digital-art-4x": "upscayl",
    "high-fidelity-4x": "upscayl",
    "remacri-4x": "upscayl",
    "ultramix-balanced-4x": "upscayl",
    "ultrasharp-4x": "upscayl",
    "upscayl-lite-4x": "upscayl",
    "upscayl-standard-4x": "upscayl",
    "realesr-animevideov3": "upscayl",
    "liveaction-v1-span-2x": "span",
    "nomos8k-span-otf-medium": "span",
    "hfa2k-span-2x": "span",
    "animejanai-v3.1-balanced": "animejanai",
    "animejanai-v3.1-sharp": "animejanai",
}


def resolve_model_name(name: str) -> str:
    raw = str(name or "").strip()
    return MODEL_MAPPING.get(raw, raw) or "digital-art-4x"


def get_model_backend(model_name: str) -> str:
    return MODEL_BACKENDS.get(model_name, "upscayl")


async def run_upscale(
    input_path: Path,
    output_path: Path,
    model_name: str,
    scale: int = 4,
    enable_taa: bool = False,
    passes: int = 1,
    config: dict = None,
) -> bool:
    config = config or {}
    resolved_model = resolve_model_name(model_name)
    backend = get_model_backend(resolved_model)

    if backend == "animejanai":
        models_dir = config.get("animejanai_models_path") or str(Path(__file__).parent / "models")
        runner_py = Path(__file__).parent / "animejanai_runner.py"

        # 多轮迭代处理 (Multi-pass)
        passes = max(1, min(6, passes))
        current_in = input_path
        temp_files: list[Path] = []
        try:
            for p_idx in range(1, passes + 1):
                is_last = p_idx == passes
                current_out = output_path if is_last else output_path.with_name(f"{output_path.stem}_pass{p_idx}.png")
                if not is_last:
                    temp_files.append(current_out)

                cmd = [
                    sys.executable,
                    str(runner_py),
                    "--input",
                    str(current_in.resolve()),
                    "--output",
                    str(current_out.resolve()),
                    "--model",
                    resolved_model,
                    "--models",
                    str(models_dir),
                    "--tile-size",
                    "768",
                    "--tile-overlap",
                    "64",
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    print(f"[错误] AnimeJaNai 执行失败 (轮次 {p_idx}/{passes}): {stderr.decode('utf-8', 'ignore')}")
                    return False
                current_in = current_out
            return output_path.exists()
        finally:
            for tf in temp_files:
                tf.unlink(missing_ok=True)

    elif backend == "span":
        span_bin = config.get("span_bin_path")
        span_models = config.get("span_models_path")
        if not span_bin or not Path(span_bin).exists():
            # 自动降级至 Upscayl remacri
            print(f"[提示] 未找到 SPAN 运行程序 ({span_bin})，回退到 Upscayl remacri-4x 执行")
            return await run_upscale(
                input_path, output_path, "remacri-4x", scale=scale, enable_taa=enable_taa, config=config
            )
        cmd = [span_bin, "-i", str(input_path.resolve()), "-o", str(output_path.resolve()), "-n", resolved_model, "-s", str(scale)]
        if span_models and Path(span_models).exists():
            cmd.extend(["-m", span_models])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return output_path.exists()

    else:
        # Upscayl 后端
        upscayl_bin = config.get("upscayl_bin_path")
        models_dir = config.get("upscayl_models_path")
        if not upscayl_bin or not Path(upscayl_bin).exists():
            print(f"[错误] 未找到 Upscayl 可执行文件: {upscayl_bin}")
            return False

        cmd = [
            upscayl_bin,
            "-i",
            str(input_path.resolve()),
            "-o",
            str(output_path.resolve()),
            "-n",
            resolved_model,
            "-s",
            str(scale),
        ]
        if enable_taa:
            cmd.append("-x")
        if models_dir and Path(models_dir).exists():
            cmd.extend(["-m", str(models_dir)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return output_path.exists()


async def run_encode(
    input_path: Path,
    output_path: Path,
    fmt: str = "AVIF",
    cpu_used: int = 1,
    crf: int = 18,
    distance: float = 1.0,
    effort: int = 9,
    config: dict = None,
) -> bool:
    config = config or {}
    ffmpeg_bin = config.get("ffmpeg_bin_path") or "ffmpeg"
    threads = str(max(1, int(config.get("ffmpeg_max_threads", 4))))

    fmt_upper = str(fmt).upper()
    cmd = [ffmpeg_bin, "-y", "-i", str(input_path.resolve())]

    if fmt_upper.startswith("JXL"):
        cmd.extend([
            "-c:v:0", "libjxl",
            "-effort:v:0", str(effort),
            "-distance:v:0", str(distance),
            "-threads", threads,
            str(output_path.resolve()),
        ])
    elif fmt_upper.startswith("JPG") or fmt_upper.startswith("JPEG"):
        cmd.extend([
            "-vf", "scale=1920:1920:force_original_aspect_ratio=decrease",
            "-q:v", "4",
            "-frames:v", "1",
            "-threads", threads,
            str(output_path.resolve()),
        ])
    else:
        # 默认 AVIF
        cmd.extend([
            "-c:v:0", "libaom-av1",
            "-cpu-used:v:0", str(cpu_used),
            "-crf:v:0", str(crf),
            "-b:v:0", "0",
            "-still-picture", "1",
            "-row-mt", "1",
            "-threads", threads,
            str(output_path.resolve()),
        ])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return output_path.exists()
