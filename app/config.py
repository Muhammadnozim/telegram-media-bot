from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Union

class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS: List[int] = []
    DATABASE_URL: str = "sqlite+aiosqlite:///bot.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    MAX_DOWNLOAD_SIZE_MB: int = 50
    RATE_LIMIT_PER_MIN: int = 10

    @field_validator("ADMIN_IDS", mode="before")
    def parse_admin_ids(cls, v: Union[str, int, List[int]]) -> List[int]:
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            v = v.strip("[]").strip()
            if not v:
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    class Config:
        env_file = ".env"

settings = Settings()
