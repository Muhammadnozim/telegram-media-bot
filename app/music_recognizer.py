import os
import aiohttp
import asyncio

MUSIC_API_KEY = os.getenv("MUSIC_API_KEY", "")

async def recognize_audio(file_path: str) -> dict:
    """AudD API orqali audio/voicedan qo'shiqni aniqlash"""
    if not MUSIC_API_KEY:
        return None
    
    url = "https://api.audd.io/"
    data = aiohttp.FormData()
    data.add_field('api_token', MUSIC_API_KEY)
    data.add_field('return', 'apple_music,spotify')
    
    with open(file_path, 'rb') as f:
        data.add_field('file', f, filename=os.path.basename(file_path))
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, data=data, timeout=30) as resp:
                    res = await resp.json()
                    if res.get("status") == "success" and res.get("result"):
                        result = res["result"]
                        return {
                            "artist": result.get("artist", "Noma'lum"),
                            "title": result.get("title", "Noma'lum"),
                            "album": result.get("album", "Noma'lum"),
                            "release_date": result.get("release_date", "Noma'lum"),
                            "score": 100
                        }
            except Exception as e:
                print(f"Music recognition error: {e}")
    return None
