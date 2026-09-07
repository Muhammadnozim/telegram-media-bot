import asyncio
import logging
from aiogram import Bot, Dispatcher
from app.config import settings
from app.database.connection import init_db
from app.handlers.user import user_router

logging.basicConfig(level=logging.INFO)

async def main():
    await init_db()
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(user_router)

    logging.info("Bot muvaffaqiyatli ishga tushdi!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
