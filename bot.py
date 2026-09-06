import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


load_dotenv()

LOGGER = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "300"))

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


def extract_first_url(text: str | None) -> str | None:
    if not text:
        return None

    match = URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).rstrip(").,!?]")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return url


def format_duration(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None

    total_seconds = int(seconds)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def build_caption(info: dict) -> str:
    title = info.get("title") or "Media"
    uploader = info.get("uploader") or info.get("channel")
    duration = format_duration(info.get("duration"))

    details = [value for value in (uploader, duration) if value]
    lines = [title[:160]]
    if details:
        lines.append(" | ".join(details)[:160])

    return "\n".join(lines)[:1024]


def pick_downloaded_file(temp_dir: Path, info: dict, ydl: YoutubeDL) -> Path:
    requested_downloads = info.get("requested_downloads") or []
    for download in requested_downloads:
        filepath = download.get("filepath")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    prepared_path = Path(ydl.prepare_filename(info))
    if prepared_path.exists():
        return prepared_path

    candidates = sorted(
        [path for path in temp_dir.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]

    raise FileNotFoundError("Yuklangan fayl topilmadi.")


def download_media(url: str, temp_dir: Path) -> tuple[dict, Path]:
    output_template = str(temp_dir / "%(title).80s-%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": output_template,
        "format": (
            "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
            "b[height<=720][ext=mp4]/"
            "b[height<=720]/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "max_filesize": MAX_FILE_BYTES,
        "noplaylist": True,
        "no_warnings": True,
        "overwrites": True,
        "quiet": True,
        "retries": 2,
        "fragment_retries": 2,
        "restrictfilenames": True,
        "socket_timeout": 30,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        if info.get("_type") == "playlist":
            entries = [entry for entry in info.get("entries", []) if entry]
            if not entries:
                raise DownloadError("Playlist ichida yuklanadigan video topilmadi.")
            info = entries[0]

        return info, pick_downloaded_file(temp_dir, info, ydl)


async def keep_chat_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        await asyncio.sleep(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "Salom! Menga Instagram, Facebook, YouTube, Pinterest, TikTok yoki boshqa "
        "ommaviy media linkini yuboring. Men videoni yuklab, shu yerga qaytaraman.\n\n"
        "Eslatma: faqat o'zingizga tegishli yoki yuklashga ruxsat berilgan kontentdan foydalaning."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "Ishlatish: bitta xabarda bitta link yuboring.\n\n"
        "Qo'llab-quvvatlanadi: YouTube, Instagram, TikTok, Facebook, Pinterest va "
        "yt-dlp tanigan ko'p ommaviy saytlar. Agar sayt qo'llab-quvvatlanmasa yoki "
        f"fayl {MAX_FILE_MB} MB dan katta bo'lsa, bot xabar beradi."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    url = extract_first_url(message.text or message.caption)
    if not url:
        await message.reply_text("Menga video yoki media sahifasining linkini yuboring.")
        return

    async with DOWNLOAD_SEMAPHORE:
        status_message = await message.reply_text("Yuklab olyapman, biroz kuting...")
        temp_dir = Path(tempfile.mkdtemp(prefix="telegram-downloader-"))
        chat_action_task = asyncio.create_task(keep_chat_action(context, chat.id))

        try:
            info, file_path = await asyncio.wait_for(
                asyncio.to_thread(download_media, url, temp_dir),
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )

            file_size = file_path.stat().st_size
            if file_size > MAX_FILE_BYTES:
                await status_message.edit_text(
                    f"Fayl juda katta: {file_size / 1024 / 1024:.1f} MB. "
                    f"Hozirgi limit: {MAX_FILE_MB} MB."
                )
                return

            caption = build_caption(info)
            suffix = file_path.suffix.lower()

            with file_path.open("rb") as media_file:
                if suffix in VIDEO_EXTENSIONS:
                    await message.reply_video(
                        video=media_file,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                    )
                elif suffix in AUDIO_EXTENSIONS:
                    await message.reply_audio(
                        audio=media_file,
                        caption=caption,
                        read_timeout=180,
                        write_timeout=180,
                    )
                else:
                    await message.reply_document(
                        document=media_file,
                        caption=caption,
                        read_timeout=180,
                        write_timeout=180,
                    )

            await status_message.delete()

        except asyncio.TimeoutError:
            await status_message.edit_text("Yuklash juda uzoq davom etdi. Boshqa yoki qisqaroq link yuboring.")
        except DownloadError as exc:
            LOGGER.info("Download failed for %s: %s", url, exc)
            await status_message.edit_text(
                "Bu linkni yuklab bo'lmadi. Video ommaviy ekanini va link to'g'ri ekanini tekshiring."
            )
        except TelegramError as exc:
            LOGGER.warning("Telegram upload failed: %s", exc)
            await status_message.edit_text(
                "Fayl yuklandi, lekin Telegramga yuborishda xatolik bo'ldi. Fayl hajmi katta bo'lishi mumkin."
            )
        except Exception:
            LOGGER.exception("Unexpected error while handling %s", url)
            await status_message.edit_text("Kutilmagan xatolik bo'ldi. Keyinroq yana urinib ko'ring.")
        finally:
            chat_action_task.cancel()
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN .env faylida yoki muhit o'zgaruvchisida berilmagan.")

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    LOGGER.info("Bot ishga tushdi.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    while True:
        try:
            main()
            break
        except Conflict:
            LOGGER.warning("Telegram polling conflict. 20 soniyadan keyin qayta uriniladi.")
            time.sleep(20)
