import asyncio
import yt_dlp
import os
import uuid
from typing import Dict, Any, Optional

class MediaDownloader:
    def __init__(self, download_dir: str = "downloads"):
        self.download_dir = download_dir
        os.makedirs(self.download_dir, exist_ok=True)

    def extract_info(self, url: str) -> Optional[Dict[str, Any]]:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return info
            except Exception:
                return None

    async def get_info_async(self, url: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.extract_info, url)

    def _download_media(self, url: str, format_type: str, file_id: str) -> str:
        out_template = os.path.join(self.download_dir, f"{file_id}.%(ext)s")
        
        ydl_opts = {
            'outtmpl': out_template,
            'quiet': True,
            'no_warnings': True,
        }

        if format_type == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
            expected_ext = "mp3"
        else:
            ydl_opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            })
            expected_ext = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        file_path = os.path.join(self.download_dir, f"{file_id}.{expected_ext}")
        if not os.path.exists(file_path):
            # Fallback for unexpected formats
            for f in os.listdir(self.download_dir):
                if f.startswith(file_id):
                    return os.path.join(self.download_dir, f)
        return file_path

    async def download_async(self, url: str, format_type: str) -> str:
        file_id = str(uuid.uuid4())
        return await asyncio.to_thread(self._download_media, url, format_type, file_id)

downloader = MediaDownloader()
