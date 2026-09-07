import os
import re
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r'https?://[^\s]+')

# YouTube va boshqa saytlar bloklamasligi uchun umumiy headers sozlamalari
COMMON_YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'geo_bypass': True,
    'headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
}

def search_youtube_tracks(query: str, max_results: int = 10):
    """Matn bo'yicha YouTube'dan 10 tagacha qo'shiq qidirish"""
    ydl_opts = {
        **COMMON_YDL_OPTS,
        'format': 'bestaudio/best',
        'extract_flat': True,
        'skip_download': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            entries = info.get('entries', []) if info else []
            
            if not entries:
                info = ydl.extract_info(f"ytmusicsearch{max_results}:{query}", download=False)
                entries = info.get('entries', []) if info else []

            results = []
            for entry in entries:
                if entry:
                    results.append({
                        'id': entry.get('id'),
                        'title': entry.get('title', 'Noma\'lum qo\'shiq'),
                        'uploader': entry.get('uploader', 'Noma\'lum artist'),
                        'duration': entry.get('duration', 0),
                        'url': f"https://www.youtube.com/watch?v={entry.get('id')}"
                    })
            return results
        except Exception as e:
            logger.error(f"Qidiruvda xatolik: {e}")
            return []

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xush kelibsiz!\n\n"
        "1. **Istalgan saytdan video yuklash:** Video havolasini (Instagram, TikTok, YouTube va b.) yuboring.\n"
        "2. **Musiqa qidirish:** Qo'shiq yoki artist nomini yozing."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message and update.message.text else ""

    if not text:
        return

    # 1. AGAR HAVOLA (LINK) YUBORILSA
    if URL_REGEX.search(text):
        url = URL_REGEX.search(text).group(0)
        msg = await update.message.reply_text("🎬 Video tahlil qilinmoqda va yuklanmoqda...")

        ydl_opts = {
            **COMMON_YDL_OPTS,
            'format': 'bestvideo[filesize<=45M][ext=mp4]+bestaudio/best[filesize<=45M]/best',
            'outtmpl': 'downloads/%(id)s.%(ext)s',
        }

        try:
            loop = asyncio.get_event_loop()
            def download_any_video():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    return filename, info.get('id', 'video'), info.get('webpage_url', url)

            filename, video_id, web_url = await loop.run_in_executor(None, download_any_video)

            if not os.path.exists(filename):
                # Ba'zida kengaytma o'zgarishi mumkin
                base_path = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.mkv', '.webm']:
                    if os.path.exists(base_path + ext):
                        filename = base_path + ext
                        break

            file_size_mb = os.path.getsize(filename) / (1024 * 1024)

            if file_size_mb > MAX_FILE_SIZE_MB:
                await msg.edit_text(
                    f"⚠️ **Fayl hajmi juda katta ({file_size_mb:.1f} MB)!**\n\n"
                    f"Telegram botlar rasman ko'pida 50 MB fayl yubora oladi."
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            context.user_data[f"url_{video_id}"] = web_url
            keyboard = [[InlineKeyboardButton("🎵 Musiqasini yuklash (MP3)", callback_data=f"dl_audio:{video_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            with open(filename, 'rb') as video_file:
                await update.message.reply_video(video=video_file, reply_markup=reply_markup)

            await msg.delete()
            if os.path.exists(filename):
                os.remove(filename)

        except Exception as e:
            logger.error(f"Video yuklashda xatolik: {e}")
            await msg.edit_text("❌ Videoni yuklab bo'lmadi. Havola noto'g'ri yoki fayl juda katta.")
        return

    # 2. AGAR SHUNCHAKI MATN YOZILSA — Qidiruv
    msg = await update.message.reply_text("🔍 Musiqa qidirilmoqda...")
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_youtube_tracks, text, 10)

    if not results:
        await msg.edit_text("❌ Hech narsa topilmadi.")
        return

    context.user_data['search_results'] = {res['id']: res['url'] for res in results}

    response_text = f"🔍 <b>\"{text}\" bo'yicha natijalar:</b>\n\n"
    buttons = []
    row = []

    for idx, item in enumerate(results, 1):
        duration_min = f"{item['duration'] // 60}:{item['duration'] % 60:02d}" if item['duration'] else ""
        response_text += f"{idx}. <b>{item['title']}</b> - {item['uploader']} [{duration_min}]\n"

        row.append(InlineKeyboardButton(str(idx), callback_data=f"song_idx:{item['id']}"))
        if len(row) == 5:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    await msg.edit_text(response_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("song_idx:"):
        video_id = data.split(":")[1]
        video_url = context.user_data.get('search_results', {}).get(video_id) or f"https://www.youtube.com/watch?v={video_id}"

        msg = await query.message.reply_text("🎧 Audio yuklanmoqda, kuting...")
        await download_and_send_audio(query.message, msg, video_url)

    elif data.startswith("dl_audio:"):
        video_id = data.split(":")[1]
        video_url = context.user_data.get(f"url_{video_id}") or f"https://www.youtube.com/watch?v={video_id}"

        msg = await query.message.reply_text("🎧 Musiqa ajratib olinmoqda...")
        await download_and_send_audio(query.message, msg, video_url)

async def download_and_send_audio(message, status_msg, url: str):
    ydl_opts = {
        **COMMON_YDL_OPTS,
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(id)s.%(ext)s',
    }

    try:
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                return filename, info.get('title', 'Musiqa'), info.get('uploader', 'Artist')

        audio_file, title, uploader = await loop.run_in_executor(None, extract)

        if not os.path.exists(audio_file):
            base_path = os.path.splitext(audio_file)[0]
            for ext in ['.webm', '.m4a', '.mp3', '.opus']:
                if os.path.exists(base_path + ext):
                    audio_file = base_path + ext
                    break

        with open(audio_file, 'rb') as audio:
            await message.reply_audio(audio=audio, title=title, performer=uploader)

        await status_msg.delete()
        if os.path.exists(audio_file):
            os.remove(audio_file)

    except Exception as e:
        logger.error(f"Audio yuklashda xatolik: {e}")
        await status_msg.edit_text("❌ Audioni yuklab bo'lmadi.")

def main():
    if not os.path.exists("downloads"):
        os.makedirs("downloads")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()
