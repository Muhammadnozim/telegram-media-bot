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
# Telegram Bot API cheklovi: max 50 MB
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# URL ekanligini aniqlash uchun regex
URL_REGEX = re.compile(r'https?://[^\s]+')

def search_youtube_tracks(query: str, max_results: int = 10):
    """Matn bo'yicha YouTube'dan 10 tagacha qo'shiq qidirish"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'extract_flat': True,
        'default_search': f'ytsearch{max_results}',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            entries = info.get('entries', [])
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
        "1. **Istalgan saytdan video yuklash:** Video havolasini (Instagram, TikTok, YouTube, Google va b.) yuboring.\n"
        "2. **Musiqa qidirish:** Qo'shiq yoki artist nomini yozing."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message and update.message.text else ""

    if not text:
        return

    # 1. AGAR HAVOLA (LINK) YUBORILSA — Istalgan saytdan videoni yuklash
    if URL_REGEX.search(text):
        url = URL_REGEX.search(text).group(0)
        msg = await update.message.reply_text("🎬 Video tahlil qilinmoqda va yuklanmoqda...")

        # universal yt-dlp sozlamalari (istalgan sayt uchun)
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }

        try:
            loop = asyncio.get_event_loop()
            def download_any_video():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    return filename, info.get('id', 'video'), info.get('webpage_url', url)

            filename, video_id, web_url = await loop.run_in_executor(None, download_any_video)

            # Fayl hajmini tekshirish (Telegram Bot API 50 MB limiti)
            file_size_mb = os.path.getsize(filename) / (1024 * 1024)

            if file_size_mb > MAX_FILE_SIZE_MB:
                await msg.edit_text(
                    f"⚠️ **Fayl hajmi juda katta ({file_size_mb:.1f} MB)!**\n\n"
                    f"Telegram botlar rasman ko'pida 50 MB fayl yubora oladi. "
                    f"Iltimos, pastroq sifatli havola yuboring yoki faqat audiosini yuklang."
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            # Musiqasini ajratib olish tugmasi
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
            await msg.edit_text("❌ Ushbu saytdan videoni yuklab bo'lmadi yoki havola xato.")
        return

    # 2. AGAR SHUNCHAKI MATN YOZILSA — VKM bot kabi 10 ta musiqa chiqarish
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

        await query.message.reply_text("🎧 Audio yuklanmoqda, kuting...")
        await download_and_send_audio(query.message, video_url)

    elif data.startswith("dl_audio:"):
        video_id = data.split(":")[1]
        video_url = context.user_data.get(f"url_{video_id}") or f"https://www.youtube.com/watch?v={video_id}"

        await query.message.reply_text("🎧 Musiqa ajratib olinmoqda...")
        await download_and_send_audio(query.message, video_url)

async def download_and_send_audio(message, url: str):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'downloads/%(id)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True
    }

    try:
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                mp3_filename = os.path.splitext(filename)[0] + ".mp3"
                return mp3_filename, info.get('title'), info.get('uploader')

        mp3_file, title, uploader = await loop.run_in_executor(None, extract)

        with open(mp3_file, 'rb') as audio:
            await message.reply_audio(audio=audio, title=title, performer=uploader)

        if os.path.exists(mp3_file):
            os.remove(mp3_file)

    except Exception as e:
        logger.error(f"Audio yuklashda xatolik: {e}")
        await message.reply_text("❌ Audioni yuklab bo'lmadi.")

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
