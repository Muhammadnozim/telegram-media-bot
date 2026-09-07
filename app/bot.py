import asyncio
import os
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv
from app.database import init_db
from app.handlers import router

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def main():
    # Ma'lumotlar bazasini yaratish
    init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    dp.include_router(router)
    
    print("Bot muvaffaqiyatli ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
