import yt_dlp
import os
import uuid

def extract_media_info(url: str):
    """Media haqida ma'lumot va mavjud sifatlar/hajmlarni olish"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        
        formats = []
        if 'formats' in info:
            for f in info['formats']:
                # Video formatlarni ajratib olish
                if f.get('vcodec') != 'none':
                    res = f.get('height') or f.get('format_note') or 'SD'
                    filesize = f.get('filesize') or f.get('filesize_approx') or 0
                    filesize_mb = round(filesize / (1024 * 1024), 1) if filesize else 0
                    
                    formats.append({
                        'format_id': f['format_id'],
                        'resolution': f'{res}p' if isinstance(res, int) else str(res),
                        'ext': f.get('ext', 'mp4'),
                        'filesize_mb': filesize_mb
                    })
        
        # Eng yaxshi 4-5 ta sifatni saqlash
        unique_formats = {f['resolution']: f for f in formats if f['filesize_mb'] > 0}.values()
        
        return {
            'title': info.get('title', 'Video'),
            'duration': f"{info.get('duration', 0) // 60}:{info.get('duration', 0) % 60:02d}",
            'formats': list(unique_formats)[:5]
        }

def download_video_format(url: str, format_id: str = None) -> str:
    """Belgilangan format bo'yicha videoni temp faylga yuklash"""
    temp_filename = f"temp_{uuid.uuid4().hex}.mp4"
    
    ydl_opts = {
        'format': f"{format_id}+bestaudio/best" if format_id else 'bestvideo+bestaudio/best',
        'outtmpl': temp_filename,
        'quiet': True,
        'merge_output_format': 'mp4'
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
        
    return temp_filename
