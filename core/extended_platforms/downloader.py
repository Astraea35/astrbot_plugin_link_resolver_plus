'''Download media from extended platforms through maintained extractor tools.'''

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .specs import ExtendedPlatform


_MEDIA_SUFFIXES = {
    '.3gp', '.aac', '.avi', '.flac', '.gif', '.jpeg', '.jpg', '.m4a', '.m4v',
    '.mkv', '.mov', '.mp3', '.mp4', '.ogg', '.opus', '.png', '.wav', '.webm',
    '.webp',
}
_IGNORED_SUFFIXES = {'.description', '.info.json', '.part', '.tmp', '.ytdl'}


@dataclass(slots=True)
class DownloadResult:
    '''Normalized output from either download engine.'''

    title: str
    author: str
    source_url: str
    files: list[Path]
    engine: str


class ExtendedDownloadError(RuntimeError):
    '''Raised when neither supported download engine yields media files.'''


class ExtendedMediaDownloader:
    '''Best-effort media downloader with yt-dlp and gallery-dl fallback.'''

    def __init__(self, output_root: Path):
        self.output_root = output_root

    async def download(
        self,
        url: str,
        platform: ExtendedPlatform,
        request_id: str,
        *,
        proxy_url: str = '',
        cookies_file: str = '',
        timeout_seconds: int = 120,
        gallery_dl_fallback: bool = True,
    ) -> DownloadResult:
        job_dir = self.output_root / platform.key / request_id
        return await asyncio.wait_for(
            asyncio.to_thread(
                self._download_sync,
                url,
                platform,
                job_dir,
                proxy_url,
                cookies_file,
                timeout_seconds,
                gallery_dl_fallback,
            ),
            timeout=max(10, timeout_seconds + 15),
        )

    def _download_sync(
        self,
        url: str,
        platform: ExtendedPlatform,
        job_dir: Path,
        proxy_url: str,
        cookies_file: str,
        timeout_seconds: int,
        gallery_dl_fallback: bool,
    ) -> DownloadResult:
        job_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        engines = ['gallery-dl', 'yt-dlp'] if platform.gallery_dl_first else ['yt-dlp', 'gallery-dl']
        if not gallery_dl_fallback:
            engines = ['yt-dlp']

        for engine in engines:
            try:
                if engine == 'yt-dlp':
                    result = self._download_with_ytdlp(
                        url, job_dir, proxy_url, cookies_file, timeout_seconds
                    )
                else:
                    result = self._download_with_gallery_dl(
                        url, job_dir, proxy_url, timeout_seconds
                    )
                if result.files:
                    return result
            except Exception as exc:
                errors.append(f'{engine}: {exc}')

        detail = '; '.join(errors) if errors else '未发现可下载媒体'
        raise ExtendedDownloadError(detail)

    def _download_with_ytdlp(
        self,
        url: str,
        job_dir: Path,
        proxy_url: str,
        cookies_file: str,
        timeout_seconds: int,
    ) -> DownloadResult:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            raise ExtendedDownloadError('未安装 yt-dlp') from exc

        options: dict[str, Any] = {
            'format': 'bestvideo*+bestaudio/best',
            'ignoreerrors': True,
            'noplaylist': False,
            'no_warnings': True,
            'outtmpl': str(job_dir / '%(extractor)s_%(id)s_%(autonumber)02d.%(ext)s'),
            'overwrites': False,
            'quiet': True,
            'retries': 3,
            'fragment_retries': 3,
            'socket_timeout': timeout_seconds,
            'writethumbnail': False,
            'writeinfojson': False,
        }
        if proxy_url:
            options['proxy'] = proxy_url
        if cookies_file and Path(cookies_file).is_file():
            options['cookiefile'] = cookies_file

        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True) or {}

        title, author, source_url = self._extract_metadata(info, url)
        return DownloadResult(
            title=title,
            author=author,
            source_url=source_url,
            files=self._collect_media_files(job_dir),
            engine='yt-dlp',
        )

    def _download_with_gallery_dl(
        self,
        url: str,
        job_dir: Path,
        proxy_url: str,
        timeout_seconds: int,
    ) -> DownloadResult:
        executable = shutil.which('gallery-dl') or shutil.which('gallery-dl.exe')
        if not executable:
            raise ExtendedDownloadError('未安装 gallery-dl')

        command = [executable, '-D', str(job_dir)]
        if proxy_url:
            command.extend(['-o', f'extractor.*.proxy={proxy_url}'])
        command.append(url)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode not in (0, 1):
            message = (completed.stderr or completed.stdout).strip()
            raise ExtendedDownloadError(message or f'gallery-dl 退出码 {completed.returncode}')

        return DownloadResult(
            title='',
            author='',
            source_url=url,
            files=self._collect_media_files(job_dir),
            engine='gallery-dl',
        )

    @staticmethod
    def _extract_metadata(info: dict[str, Any], fallback_url: str) -> tuple[str, str, str]:
        entries = info.get('entries') if isinstance(info, dict) else None
        first = next((entry for entry in entries or [] if isinstance(entry, dict)), None)
        item = first or info if isinstance(info, dict) else {}
        title = str(item.get('title') or item.get('fulltitle') or '')
        author = str(item.get('uploader') or item.get('channel') or item.get('artist') or '')
        source_url = str(item.get('webpage_url') or item.get('original_url') or fallback_url)
        return title, author, source_url

    @staticmethod
    def _collect_media_files(job_dir: Path) -> list[Path]:
        files = []
        for path in job_dir.rglob('*'):
            if not path.is_file() or path.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            if path.suffix.lower() in _MEDIA_SUFFIXES and path.stat().st_size > 0:
                files.append(path)
        return sorted(files, key=lambda item: item.stat().st_mtime)


__all__ = ['DownloadResult', 'ExtendedDownloadError', 'ExtendedMediaDownloader']
