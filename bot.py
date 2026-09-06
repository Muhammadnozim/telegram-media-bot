import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


load_dotenv()

LOGGER = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
SPOTIFY_HOSTS = {"spotify.com", "open.spotify.com", "www.spotify.com", "spotify.link", "spotify.app.link"}
SPOTIFY_UNSUPPORTED_TEXT = (
    "Spotify linkidan musiqa yoki video faylini yuklab bera olmayman.\n\n"
    "Spotify kontenti yuklab olish yoki stream ripping uchun ruxsat bermaydi. "
    "Spotify'da tinglang yoki yuklashga ruxsatli boshqa ommaviy media link yuboring."
)


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


def get_audio_format() -> str:
    audio_format = os.getenv("AUDIO_FORMAT", "mp3").strip().lower()
    if audio_format in {"mp3", "m4a", "opus"}:
        return audio_format

    LOGGER.warning("AUDIO_FORMAT noto'g'ri berilgan. mp3 ishlatiladi.")
    return "mp3"


MAX_FILE_MB = max(1, get_int_env("MAX_FILE_MB", 45))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = max(1, get_int_env("MAX_CONCURRENT_DOWNLOADS", 2))
DOWNLOAD_TIMEOUT_SECONDS = max(1, get_int_env("DOWNLOAD_TIMEOUT_SECONDS", 300))
MAX_DAILY_DOWNLOADS = get_int_env("MAX_DAILY_DOWNLOADS", 5)
AUDIO_FORMAT = get_audio_format()
AUDIO_QUALITY_KBPS = min(320, max(64, get_int_env("AUDIO_QUALITY_KBPS", 192)))
ADMIN_IDS = parse_admin_ids()
LIMIT_TIMEZONE = os.getenv("LIMIT_TIMEZONE", "Asia/Tashkent")
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
STATE_LOCK = asyncio.Lock()
AUDIO_COMMANDS = {"/audio", "/mp3", "/music"}
MAX_AUDIO_REQUESTS = 200


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
    return {"users": {}, "daily_usage": {}, "settings": {}, "audio_requests": {}}


def normalize_state(state: object) -> dict:
    if not isinstance(state, dict):
        state = {}
    state.setdefault("users", {})
    state.setdefault("daily_usage", {})
    state.setdefault("settings", {})
    state.setdefault("audio_requests", {})
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


def prune_audio_requests(requests: dict) -> None:
    if len(requests) <= MAX_AUDIO_REQUESTS:
        return

    ordered_ids = sorted(requests, key=lambda request_id: requests[request_id].get("created_at", ""))
    for request_id in ordered_ids[: len(requests) - MAX_AUDIO_REQUESTS]:
        requests.pop(request_id, None)


def remember_audio_request(state: dict, update: Update, url: str) -> str:
    request_id = secrets.token_urlsafe(8)
    user = update.effective_user
    requests = state.setdefault("audio_requests", {})
    requests[request_id] = {
        "url": url,
        "user_id": user.id if user else None,
        "created_at": now_iso(),
    }
    prune_audio_requests(requests)
    return request_id


async def build_audio_button(update: Update, url: str) -> InlineKeyboardMarkup:
    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        request_id = remember_audio_request(state, update, url)
        save_state(state)

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Musiqasini yuklash", callback_data=f"audio:{request_id}")]]
    )


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


def is_spotify_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    return host in SPOTIFY_HOSTS or host.endswith(".spotify.com")


def first_command(text: str | None) -> str:
    if not text:
        return ""

    command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command


def safe_filename(name: str | None, default: str) -> str:
    if not name:
        return default

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or default


def get_video_attachment(message):
    if message.video:
        filename = safe_filename(message.video.file_name, f"{message.video.file_unique_id}.mp4")
        return message.video, filename

    document = message.document
    if document and (document.mime_type or "").startswith("video/"):
        filename = safe_filename(document.file_name, f"{document.file_unique_id}.mp4")
        return document, filename

    return None, None


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


def download_audio(url: str, temp_dir: Path) -> tuple[dict, Path]:
    output_template = str(temp_dir / "%(title).80s-%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "max_filesize": MAX_FILE_BYTES,
        "noplaylist": True,
        "no_warnings": True,
        "overwrites": True,
        "quiet": True,
        "retries": 2,
        "fragment_retries": 2,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": AUDIO_FORMAT,
                "preferredquality": str(AUDIO_QUALITY_KBPS),
            }
        ],
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        if info.get("_type") == "playlist":
            entries = [entry for entry in info.get("entries", []) if entry]
            if not entries:
                raise DownloadError("Playlist ichida yuklanadigan audio topilmadi.")
            info = entries[0]

        return info, pick_downloaded_file(temp_dir, info, ydl)


def ffmpeg_audio_args() -> tuple[list[str], str]:
    bitrate = f"{AUDIO_QUALITY_KBPS}k"
    if AUDIO_FORMAT == "m4a":
        return ["-codec:a", "aac", "-b:a", bitrate], ".m4a"
    if AUDIO_FORMAT == "opus":
        return ["-codec:a", "libopus", "-b:a", bitrate], ".opus"
    return ["-codec:a", "libmp3lame", "-b:a", bitrate], ".mp3"


def extract_audio_from_video(input_path: Path, temp_dir: Path) -> Path:
    audio_args, extension = ffmpeg_audio_args()
    output_path = temp_dir / f"{safe_filename(input_path.stem, 'audio')}{extension}"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        *audio_args,
        str(output_path),
    ]

    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 or not output_path.exists():
        LOGGER.warning("ffmpeg audio extraction failed: %s", completed.stderr[-1000:])
        raise RuntimeError("Videodan musiqa ajratib bo'lmadi.")

    return output_path


async def keep_chat_action(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    action: str = ChatAction.UPLOAD_VIDEO,
) -> None:
    while True:
        await context.bot.send_chat_action(chat_id=chat_id, action=action)
        await asyncio.sleep(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await track_user(update)
    await message.reply_text(
        "Salom! Menga Instagram, Facebook, YouTube, Pinterest, TikTok yoki boshqa "
        "ommaviy media linkini yuboring. Men videoni yuklab, shu yerga qaytaraman.\n\n"
        "Video tagidagi Musiqasini yuklash tugmasi orqali ovozini MP3 qilib olasiz. "
        "Faqat musiqa kerak bo'lsa: /audio link\n\n"
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
        "Video tagidagi Musiqasini yuklash tugmasi audioni MP3 qilib beradi.\n"
        "Faqat audio: /audio link\n"
        "Yuklangan videodan audio: videoni /audio caption bilan yuboring.\n"
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


async def send_downloaded_file(
    message,
    file_path: Path,
    caption: str,
    *,
    force_audio: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    suffix = file_path.suffix.lower()

    with file_path.open("rb") as media_file:
        if force_audio or suffix in AUDIO_EXTENSIONS:
            await message.reply_audio(
                audio=media_file,
                caption=caption,
                reply_markup=reply_markup,
                read_timeout=180,
                write_timeout=180,
            )
        elif suffix in VIDEO_EXTENSIONS:
            await message.reply_video(
                video=media_file,
                caption=caption,
                reply_markup=reply_markup,
                supports_streaming=True,
                read_timeout=180,
                write_timeout=180,
            )
        else:
            await message.reply_document(
                document=media_file,
                caption=caption,
                reply_markup=reply_markup,
                read_timeout=180,
                write_timeout=180,
            )


async def process_url_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    audio_only: bool = False,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    if is_spotify_url(url):
        await track_user(update)
        await message.reply_text(SPOTIFY_UNSUPPORTED_TEXT)
        return

    block_message = await limit_block_message(update)
    if block_message:
        await message.reply_text(block_message)
        return

    async with DOWNLOAD_SEMAPHORE:
        status_text = "Musiqasini yuklab olyapman, biroz kuting..." if audio_only else "Yuklab olyapman, biroz kuting..."
        status_message = await message.reply_text(status_text)
        temp_dir = Path(tempfile.mkdtemp(prefix="telegram-downloader-"))
        action = ChatAction.UPLOAD_DOCUMENT if audio_only else ChatAction.UPLOAD_VIDEO
        chat_action_task = asyncio.create_task(keep_chat_action(context, chat.id, action))

        try:
            download_func = download_audio if audio_only else download_media
            info, file_path = await asyncio.wait_for(
                asyncio.to_thread(download_func, url, temp_dir),
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
            reply_markup = None
            if not audio_only and file_path.suffix.lower() in VIDEO_EXTENSIONS:
                reply_markup = await build_audio_button(update, url)

            await send_downloaded_file(
                message,
                file_path,
                caption,
                force_audio=audio_only,
                reply_markup=reply_markup,
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


async def process_uploaded_video_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    attachment, filename = get_video_attachment(message)
    if not attachment or not filename:
        await message.reply_text("Videodan musiqa ajratish uchun video fayl yuboring yoki /audio link yozing.")
        return

    block_message = await limit_block_message(update)
    if block_message:
        await message.reply_text(block_message)
        return

    if attachment.file_size and attachment.file_size > MAX_FILE_BYTES:
        await message.reply_text(
            f"Video juda katta: {attachment.file_size / 1024 / 1024:.1f} MB. "
            f"Hozirgi limit: {MAX_FILE_MB} MB."
        )
        return

    async with DOWNLOAD_SEMAPHORE:
        status_message = await message.reply_text("Videodan musiqasini ajratyapman, biroz kuting...")
        temp_dir = Path(tempfile.mkdtemp(prefix="telegram-audio-"))
        chat_action_task = asyncio.create_task(keep_chat_action(context, chat.id, ChatAction.UPLOAD_DOCUMENT))

        try:
            input_path = temp_dir / filename
            telegram_file = await attachment.get_file()
            await telegram_file.download_to_drive(custom_path=str(input_path))

            audio_path = await asyncio.wait_for(
                asyncio.to_thread(extract_audio_from_video, input_path, temp_dir),
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )

            file_size = audio_path.stat().st_size
            if file_size > MAX_FILE_BYTES:
                await status_message.edit_text(
                    f"Audio juda katta: {file_size / 1024 / 1024:.1f} MB. "
                    f"Hozirgi limit: {MAX_FILE_MB} MB."
                )
                return

            caption = f"{Path(filename).stem[:160]}\nAudio"
            await send_downloaded_file(message, audio_path, caption, force_audio=True)
            await mark_successful_download(update)
            await status_message.delete()

        except asyncio.TimeoutError:
            await status_message.edit_text("Audio ajratish juda uzoq davom etdi. Qisqaroq video yuboring.")
        except TelegramError as exc:
            LOGGER.warning("Telegram file/audio failed: %s", exc)
            await status_message.edit_text("Videoni olish yoki audioni yuborishda xatolik bo'ldi.")
        except Exception:
            LOGGER.exception("Unexpected error while extracting uploaded video audio")
            await status_message.edit_text("Videodan musiqa ajratib bo'lmadi.")
        finally:
            chat_action_task.cancel()
            shutil.rmtree(temp_dir, ignore_errors=True)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    url = extract_first_url(" ".join(context.args) or message.text or message.caption)
    if url:
        await process_url_download(update, context, url, audio_only=True)
        return

    await process_uploaded_video_audio(update, context)


async def audio_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data:
        return

    request_id = query.data.split(":", 1)[1]
    async with STATE_LOCK:
        state = load_state()
        upsert_user(state, update)
        record = state.setdefault("audio_requests", {}).get(request_id)
        save_state(state)

    if not record:
        await query.answer("Bu tugma eskirgan. Linkni qayta yuboring.", show_alert=True)
        return

    requester_id = record.get("user_id")
    if requester_id and user and requester_id != user.id and not is_admin(user.id):
        await query.answer("Bu tugma link yuborgan odam uchun.", show_alert=True)
        return

    url = record.get("url")
    if not isinstance(url, str) or not url:
        await query.answer("Link topilmadi. Qayta yuboring.", show_alert=True)
        return

    await query.answer("Musiqa yuklanmoqda...")
    await process_url_download(update, context, url, audio_only=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    url = extract_first_url(message.text or message.caption)
    if not url:
        await track_user(update)
        await message.reply_text("Menga video yoki media sahifasining linkini yuboring.")
        return

    await process_url_download(update, context, url)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    if first_command(message.caption) in AUDIO_COMMANDS:
        await process_uploaded_video_audio(update, context)
        return

    await track_user(update)


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
    app.add_handler(CommandHandler(["audio", "mp3", "music"], audio_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("setlimit", set_limit_command))
    app.add_handler(CommandHandler("resetlimit", reset_limit_command))
    app.add_handler(CallbackQueryHandler(audio_button_callback, pattern=r"^audio:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_video))
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
