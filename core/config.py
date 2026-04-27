"""
Connectome Configuration
Loads settings from environment / .env file.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List
import json


class Settings(BaseSettings):
    # Database
    # Railway provides DATABASE_URL as postgres:// — we normalize to postgresql://
    DATABASE_URL: str = "postgresql://connectome:connectome@localhost:5432/connectome"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_postgres_url(cls, v: str) -> str:
        """Railway uses postgres:// scheme; asyncpg requires postgresql://."""
        if isinstance(v, str) and v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        return v

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # OpenAI — if empty, Ora falls back to intelligent mock
    OPENAI_API_KEY: str = ""

    # JWT
    SECRET_KEY: str = "dev-secret-key-change-in-production-32chars!!"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8081",
        "exp://localhost:8081",
    ]

    # External APIs
    GOOGLE_PLACES_API_KEY: str = ""  # Optional — enables real venue search
    SERPAPI_KEY: str = ""            # Google Events via SerpAPI (set in Railway env vars)
    EVENTBRITE_TOKEN: str = ""       # Eventbrite public API token (set in Railway env vars)


    # Monetization
    FREE_TIER_DAILY_SCREENS: int = 10
    PREMIUM_PRICE_CENTS: int = 1299

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return [v]
        return v

    @property
    def has_serpapi(self) -> bool:
        return bool(self.SERPAPI_KEY)

    @property
    def has_eventbrite(self) -> bool:
        return bool(self.EVENTBRITE_TOKEN)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def has_openai(self) -> bool:
        return bool(self.OPENAI_API_KEY)

    @property
    def has_google_places(self) -> bool:
        return bool(self.GOOGLE_PLACES_API_KEY)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

