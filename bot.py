import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return int(raw_value)
    except ValueError:
        LOGGER.warning("%s noto'g'ri berilgan. Standart qiymat ishlatiladi: %s", name, default)
        return default


def parse_admin_ids() -> set[int]:
    admin_ids: set[int] = set()
    for value in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")):
        if not value:
            continue
        try:
            admin_ids.add(int(value))
        except ValueError:
            LOGGER.warning("ADMIN_IDS ichida noto'g'ri ID bor: %s", value)
    return admin_ids


MAX_FILE_MB = max(1, get_int_env("MAX_FILE_MB", 45))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = max(1, get_int_env("MAX_CONCURRENT_DOWNLOADS", 2))
DOWNLOAD_TIMEOUT_SECONDS = max(1, get_int_env("DOWNLOAD_TIMEOUT_SECONDS", 300))
MAX_DAILY_DOWNLOADS = get_int_env("MAX_DAILY_DOWNLOADS", 5)
ADMIN_IDS = parse_admin_ids()
LIMIT_TIMEZONE = os.getenv("LIMIT_TIMEZONE", "Asia/Tashkent")
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
STATE_LOCK = asyncio.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_key() -> str:
    try:
        tz = ZoneInfo(LIMIT_TIMEZONE)
    except ZoneInfoNotFoundError:
        LOGGER.warning("LIMIT_TIMEZONE topilmadi: %s. UTC ishlatiladi.", LIMIT_TIMEZONE)
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def empty_state() -> dict:
    return {"users": {}, "daily_usage": {}, "settings": {}}


def normalize_state(state: object) -> dict:
    if not isinstance(state, dict):
        state = {}
    state.setdefault("users", {})
    state.setdefault("daily_usage", {})
    state.setdefault("settings", {})
    return state


def load_state() -> dict:
    if not STATE_FILE.exists():
        return empty_state()

    try:
        return normalize_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("State faylini o'qib bo'lmadi: %s", exc)
        return empty_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
    temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(STATE_FILE)


def upsert_user(state: dict, update: Update) -> None:
    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)
    users = state.setdefault("users", {})
    record = users.setdefault(user_id, {"first_seen": now_iso(), "downloads_total": 0})
    record["last_seen"] = now_iso()
    record["username"] = user.username or ""
    record["full_name"] = user.full_name or ""


def is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS if user_id is not None else False


def daily_limit(state: dict) -> int:
    value = state.setdefault("settings", {}).get("max_daily_downloads", MAX_DAILY_DOWNLOADS)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return MAX_DAILY_DOWNLOADS


def used_today(state: dict, user_id: int) -> int:
    usage = state.setdefault("daily_usage", {}).setdefault(today_key(), {})
    try:
        return int(usage.get(str(user_id), 0))
    except (TypeError, ValueError):
        return 0


def record_successful_download(state: dict, user_id: int) -> None:
    today = today_key()
    user_key = str(user_id)
    usage = state.setdefault("daily_usage", {}).setdefault(today, {})
    usage[user_key] = used_today(state, user_id) + 1

    user_record = state.setdefault("users", {}).setdefault(user_key, {"first_seen": now_iso()})
    user_record["downloads_total"] = int(user_record.get("downloads_total", 0)) + 1
    user_record["last_download"] = now_iso()


def user_label(user_id: str, record: dict | None = None) -> str:
    record = record or {}
    username = record.get("username")
    full_name = record.get("full_name")
    if username:
        return f"@{username} ({user_id})"
    if full_name:
        return f"{full_name} ({user_id})"
    return user_id


async def track_user(update: Update) -> None:
    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        save_state(state)


async def limit_block_message(update: Update) -> str | None:
    user = update.effective_user
    if not user or is_admin(user.id):
        return None

    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        limit = daily_limit(state)
        used = used_today(state, user.id)
        save_state(state)

    if limit <= 0 or used < limit:
        return None

    return (
        f"Bugungi limit tugadi: {used}/{limit}.\n"
        "Ertaga yana urinib ko'ring yoki admin limitni oshirishi mumkin."
    )


async def mark_successful_download(update: Update) -> None:
    user = update.effective_user
    if not user or is_admin(user.id):
        return

    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        record_successful_download(state, user.id)
        save_state(state)


async def require_admin(update: Update) -> bool:
    message = update.effective_message
    user = update.effective_user
    if not message:
        return False

    if not ADMIN_IDS:
        await message.reply_text(
            "Admin panel hali yoqilmagan.\n"
            "Avval /id yuboring, chiqqan raqamni Railway Variables ichida ADMIN_IDS ga qo'ying."
        )
        return False

    if not user or not is_admin(user.id):
        await message.reply_text("Bu komanda faqat admin uchun.")
        return False

    return True


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

    await track_user(update)
    await message.reply_text(
        "Salom! Menga Instagram, Facebook, YouTube, Pinterest, TikTok yoki boshqa "
        "ommaviy media linkini yuboring. Men videoni yuklab, shu yerga qaytaraman.\n\n"
        "Eslatma: faqat o'zingizga tegishli yoki yuklashga ruxsat berilgan kontentdan foydalaning."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await track_user(update)
    await message.reply_text(
        "Ishlatish: bitta xabarda bitta link yuboring.\n\n"
        "Qo'llab-quvvatlanadi: YouTube, Instagram, TikTok, Facebook, Pinterest va "
        "yt-dlp tanigan ko'p ommaviy saytlar. Agar sayt qo'llab-quvvatlanmasa yoki "
        f"fayl {MAX_FILE_MB} MB dan katta bo'lsa, bot xabar beradi.\n\n"
        "Limitni ko'rish: /limit\n"
        "Telegram ID olish: /id"
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    await track_user(update)
    await message.reply_text(
        f"Sizning Telegram ID: {user.id}\n"
        "Admin qilish uchun shu raqamni Railway Variables ichida ADMIN_IDS ga qo'ying."
    )


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        limit = daily_limit(state)
        used = used_today(state, user.id)
        save_state(state)

    if is_admin(user.id):
        await message.reply_text("Siz adminsiz. Sizga kunlik limit qo'llanmaydi.")
        return

    if limit <= 0:
        await message.reply_text("Kunlik limit o'chirilgan.")
        return

    remaining = max(limit - used, 0)
    await message.reply_text(f"Bugungi limit: {used}/{limit}. Qolgan: {remaining}.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await require_admin(update):
        return

    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        today = today_key()
        usage = state.setdefault("daily_usage", {}).setdefault(today, {})
        total_today = sum(int(value) for value in usage.values())
        limit = daily_limit(state)
        users_count = len(state.setdefault("users", {}))
        save_state(state)

    limit_text = "limitsiz" if limit <= 0 else str(limit)
    await message.reply_text(
        "Admin panel\n\n"
        f"Kunlik limit: {limit_text}\n"
        f"Bugungi yuklashlar: {total_today}\n"
        f"Bugun faol foydalanuvchilar: {len(usage)}\n"
        f"Jami foydalanuvchilar: {users_count}\n\n"
        "Komandalar:\n"
        "/stats - statistika\n"
        "/users - oxirgi foydalanuvchilar\n"
        "/setlimit 5 - kunlik limitni o'zgartirish\n"
        "/resetlimit - bugungi limitlarni nol qilish"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await require_admin(update):
        return

    async with STATE_LOCK:
        state = load_state()
        today = today_key()
        usage = state.setdefault("daily_usage", {}).setdefault(today, {})
        users = state.setdefault("users", {})
        limit = daily_limit(state)
        top_users = sorted(usage.items(), key=lambda item: int(item[1]), reverse=True)[:5]

    top_lines = [
        f"{index}. {user_label(user_id, users.get(user_id))} - {count}"
        for index, (user_id, count) in enumerate(top_users, start=1)
    ]
    top_text = "\n".join(top_lines) if top_lines else "Hali bugun yuklash yo'q."
    limit_text = "limitsiz" if limit <= 0 else str(limit)

    await message.reply_text(
        "Statistika\n\n"
        f"Sana: {today}\n"
        f"Kunlik limit: {limit_text}\n"
        f"Bugungi jami yuklashlar: {sum(int(value) for value in usage.values())}\n"
        f"Jami foydalanuvchilar: {len(users)}\n\n"
        f"Top foydalanuvchilar:\n{top_text}"
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await require_admin(update):
        return

    async with STATE_LOCK:
        state = load_state()
        users = state.setdefault("users", {})
        latest_users = sorted(users.items(), key=lambda item: item[1].get("last_seen", ""), reverse=True)[:10]

    lines = []
    for index, (user_id, record) in enumerate(latest_users, start=1):
        downloads_total = record.get("downloads_total", 0)
        last_seen = record.get("last_seen", "-")
        lines.append(f"{index}. {user_label(user_id, record)} - {downloads_total} ta - {last_seen}")

    users_text = "\n".join(lines) if lines else "Hali foydalanuvchi yo'q."
    await message.reply_text(f"Oxirgi foydalanuvchilar:\n\n{users_text}")


async def set_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await require_admin(update):
        return

    if not context.args:
        await message.reply_text("Masalan: /setlimit 5\n0 yozsangiz limit o'chadi.")
        return

    try:
        new_limit = int(context.args[0])
    except ValueError:
        await message.reply_text("Limit faqat raqam bo'lishi kerak. Masalan: /setlimit 5")
        return

    if new_limit < 0 or new_limit > 100:
        await message.reply_text("Limit 0 dan 100 gacha bo'lsin. 0 = limitsiz.")
        return

    async with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})["max_daily_downloads"] = new_limit
        save_state(state)

    limit_text = "o'chirildi" if new_limit == 0 else f"{new_limit} ga o'zgardi"
    await message.reply_text(f"Kunlik limit {limit_text}.")


async def reset_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await require_admin(update):
        return

    target = context.args[0] if context.args else "all"
    today = today_key()

    async with STATE_LOCK:
        state = load_state()
        usage = state.setdefault("daily_usage", {}).setdefault(today, {})
        if target == "all":
            usage.clear()
            result_text = "Bugungi hamma limitlar nol qilindi."
        else:
            try:
                user_id = str(int(target))
            except ValueError:
                await message.reply_text("Masalan: /resetlimit yoki /resetlimit 123456789")
                return
            usage.pop(user_id, None)
            result_text = f"{user_id} uchun bugungi limit nol qilindi."
        save_state(state)

    await message.reply_text(result_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    url = extract_first_url(message.text or message.caption)
    if not url:
        await message.reply_text("Menga video yoki media sahifasining linkini yuboring.")
        return

    block_message = await limit_block_message(update)
    if block_message:
        await message.reply_text(block_message)
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

            await mark_successful_download(update)
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
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("setlimit", set_limit_command))
    app.add_handler(CommandHandler("resetlimit", reset_limit_command))
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
