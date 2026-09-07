from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: List[int] = []
    DATABASE_URL: str = "sqlite+aiosqlite:///bot.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_DOWNLOAD_SIZE_MB: int = 50
    RATE_LIMIT_PER_MIN: int = 10

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"

settings = Settings()
