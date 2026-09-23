"""Cancellation-safe card image writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def save_image_file(image: Any, output_path: Path, **options: Any) -> None:
    save_task = asyncio.create_task(
        asyncio.to_thread(image.save, output_path, **options)
    )
    try:
        await asyncio.shield(save_task)
    except asyncio.CancelledError:
        while not save_task.done():
            try:
                await asyncio.shield(save_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            save_task.result()
        except (Exception, asyncio.CancelledError):
            pass
        output_path.unlink(missing_ok=True)
        raise
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
