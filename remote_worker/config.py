# config.py
import json
import os
import shutil
from pathlib import Path

DEFAULT_CONFIG_FILE = Path(__file__).parent / "config.json"


def auto_detect_upscayl_bin() -> str:
    standard_paths = [
        Path("C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Upscayl" / "resources" / "bin" / "upscayl-bin.exe",
    ]
    for p in standard_paths:
        if p.is_file():
            return str(p.resolve()).replace("\\", "/")
    return ""


def auto_detect_upscayl_models() -> str:
    standard_paths = [
        Path("C:/Program Files/Upscayl/resources/models"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Upscayl" / "resources" / "models",
    ]
    for p in standard_paths:
        if p.is_dir():
            return str(p.resolve()).replace("\\", "/")
    return ""


def auto_detect_ffmpeg_bin() -> str:
    candidates = [
        Path("C:/ruanjian/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve()).replace("\\", "/")
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return str(Path(which_ffmpeg).resolve()).replace("\\", "/")
    return "ffmpeg"


def get_default_config() -> dict:
    return {
        "host": "0.0.0.0",
        "port": 8899,
        "token": "",
        "max_concurrency": 1,
        "ffmpeg_max_threads": 4,
        "e_core_only": True,
        "cpu_affinity_mask": "0xFF000",
        "process_priority": "IDLE",
        "upscayl_bin_path": auto_detect_upscayl_bin(),
        "upscayl_models_path": auto_detect_upscayl_models(),
        "span_bin_path": "",
        "span_models_path": "",
        "animejanai_models_path": "",
        "ffmpeg_bin_path": auto_detect_ffmpeg_bin(),
        "temp_dir": "",
    }


def load_config(config_path: Path = DEFAULT_CONFIG_FILE) -> dict:
    config = get_default_config()
    if config_path.is_file():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                user_conf = json.load(f)
                if isinstance(user_conf, dict):
                    config.update(user_conf)
        except Exception as exc:
            print(f"[警告] 读取配置文件 {config_path} 失败: {exc}，将使用默认配置")
    else:
        try:
            with config_path.open("w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            print(f"[初始化] 已自动生成默认配置文件: {config_path}")
        except Exception as exc:
            print(f"[警告] 写入默认配置文件失败: {exc}")
    return config
