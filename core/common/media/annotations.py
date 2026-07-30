"""User-facing annotations for image enhancement and transcoding results."""

from pathlib import Path


MODEL_DISPLAY_NAMES = {
    "digital-art-4x": "数字艺术 (digital-art-4x)",
    "high-fidelity-4x": "高保真 (high-fidelity-4x)",
    "remacri-4x": "Remacri (remacri-4x)",
    "ultramix-balanced-4x": "超混合平衡 (ultramix-balanced-4x)",
    "ultrasharp-4x": "超锐化 (ultrasharp-4x)",
    "upscayl-lite-4x": "轻量 (upscayl-lite-4x)",
    "upscayl-standard-4x": "标准 (upscayl-standard-4x)",
}


def format_file_size(size_bytes: int) -> str:
    """Return a compact, user-facing file size."""
    units = ("B", "KB", "MB", "GB")
    size = float(max(0, size_bytes))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def format_size_comparison(source_path: Path, processed_path: Path) -> str:
    """Describe the processed file size relative to its downloaded source."""
    try:
        source_size = source_path.stat().st_size
        processed_size = processed_path.stat().st_size
    except OSError:
        return "原始/转码后大小不可用"

    percentage = (processed_size / source_size * 100) if source_size else 0.0
    return (
        f"原始: {format_file_size(source_size)} -> "
        f"转码后: {format_file_size(processed_size)} ({percentage:.1f}%)"
    )


def format_image_processing_annotation(
    index: int,
    source_path: Path,
    processed_path: Path,
    was_upscaled: bool,
    image_type: str | None = None,
    target_model: str | None = None,
) -> str:
    """Build one consistent image-processing annotation line for every platform."""
    image_label = image_type or "未检测"
    size_comparison = format_size_comparison(source_path, processed_path)
    if was_upscaled:
        model_label = MODEL_DISPLAY_NAMES.get(target_model, target_model or "未记录")
        return (
            f"  • 图 {index}: 🎨 已 AI 升图 [{image_label} | {model_label}] "
            f"[{size_comparison}]"
        )
    return f"  • 图 {index}: ⚡ 原始画质 [{image_label}] [{size_comparison}]"


def build_image_processing_annotation_text(lines: list[str]) -> str | None:
    """Build the platform-neutral annotation message, or nothing for empty input."""
    if not lines:
        return None
    return "📊 图片 AI 升图处理标注：\n" + "\n".join(lines)
