from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from app.keyboards.user_kb import get_start_kb, get_format_kb
from app.services.downloader import downloader
from aiogram.fsm.context import FSMContext
import os

user_router = Router()

@user_router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        f"👋 Salom, {message.from_user.full_name}!\n\n"
        "🎬 Men Telegram Media Downloader Botman.\n"
        "Menga YouTube, Instagram, TikTok va boshqa platformalardan media link yuboring."
    )
    await message.answer(text, reply_markup=get_start_kb())

@user_router.message(F.text.startswith("http"))
async def handle_url(message: Message, state: FSMContext):
    url = message.text.strip()
    status_msg = await message.answer("🔍 Media ma'lumotlari tahlil qilinmoqda...")
    
    info = await downloader.get_info_async(url)
    if not info:
        await status_msg.edit_text("❌ Ushbu linkdan media topilmadi yoki havola xato.")
        return

    await state.update_data(url=url, title=info.get("title", "Media"))
    await status_msg.edit_text(
        f"📌 <b>{info.get('title', 'Media')}</b>\n\nYuklab olish formatini tanlang:",
        parse_mode="HTML",
        reply_markup=get_format_kb(url)
    )

@user_router.callback_query(F.data.startswith("dl:"))
async def process_download(callback: CallbackQuery, state: FSMContext):
    format_type = callback.data.split(":")[1]
    data = await state.get_data()
    url = data.get("url")

    if not url:
        await callback.message.edit_text("❌ Seans muddati tugagan. Linkni qayta yuboring.")
        return

    await callback.message.edit_text("⬇️ Yuklab olinmoqda, kuting...")
    
    try:
        file_path = await downloader.download_async(url, format_type)
        await callback.message.edit_text("📤 Telegram'ga yuklanmoqda...")
        
        from aiogram.types import FSInputFile
        input_file = FSInputFile(file_path)

        if format_type == "audio":
            await callback.message.answer_audio(audio=input_file)
        else:
            await callback.message.answer_video(video=input_file)
            
        await callback.message.delete()
        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        await callback.message.edit_text(f"❌ Yuklab olishda xatolik yuz berdi: {str(e)}")
