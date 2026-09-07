from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="settings"),
         InlineKeyboardButton(text="📊 Statistikam", callback_data="my_stats")],
        [InlineKeyboardButton(text="ℹ️ Yordam", callback_data="help")]
    ])

def get_format_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Video (MP4)", callback_data="dl:video"),
         InlineKeyboardButton(text="🎵 Audio (MP3)", callback_data="dl:audio")]
    ])
