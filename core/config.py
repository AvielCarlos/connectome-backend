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

    # Anthropic — enables Claude model benchmarking and switching
    ANTHROPIC_API_KEY: str = ""

    # Model override — set by ModelEvolutionAgent when a better model is found
    # Supports both OpenAI model IDs (gpt-4o) and Anthropic model IDs (claude-sonnet-4-6)
    ORA_MODEL_OVERRIDE: str = ""

    # GitHub — for daily identity pack commits (backup redundancy)
    GITHUB_TOKEN: str = ""

    # Railway — for Railway API access (redeploy, env var updates)
    RAILWAY_API_TOKEN: str = ""
    RAILWAY_SERVICE_ID: str = "088d77ed-a707-4dc4-af68-866bf99a1d63"
    RAILWAY_PROJECT_ID: str = "ab771963-d525-4b99-85e4-f084f065b0ae"

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

    # Google OAuth (for Sign In with Google + Drive integration)
    # Setup: console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0 Client ID
    # Authorized redirect URI: https://connectome-api-production.up.railway.app/api/auth/google/callback
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://connectome-api-production.up.railway.app/api/auth/google/callback"


    # Monetization — Legacy
    FREE_TIER_DAILY_SCREENS: int = 10
    PREMIUM_PRICE_CENTS: int = 1299

    # Stripe — set these in Railway env vars after creating products at dashboard.stripe.com
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_EXPLORER_MONTHLY: str = ""
    STRIPE_PRICE_EXPLORER_YEARLY: str = ""
    STRIPE_PRICE_SOVEREIGN_MONTHLY: str = ""
    STRIPE_PRICE_SOVEREIGN_YEARLY: str = ""

    # Admin emails (comma-separated) — these users get admin privileges
    ADMIN_EMAILS: str = "carlosandromeda8@gmail.com"

    @property
    def admin_email_list(self) -> list:
        return [e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()]

    @property
    def has_stripe(self) -> bool:
        return bool(self.STRIPE_SECRET_KEY)

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


