"""
Connectome Configuration
Loads settings from environment / .env file.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List
import json
import os


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
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "https://connectome-api-production.up.railway.app/api/auth/github/callback"

    # Ora brain backup freshness
    ORA_BACKUP_SCHEDULER_ENABLED: bool = True
    ORA_IDENTITY_BACKUP_INTERVAL_SECONDS: int = 3600       # hourly identity pack
    ORA_BACKUP_FRESHNESS_CHECK_SECONDS: int = 1800         # 30-minute monitor
    ORA_EVENT_BACKUP_DEBOUNCE_SECONDS: int = 900           # coalesce bursty brain changes

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
        "https://avielcarlos.github.io",
        "https://atdao.org",
        "https://www.atdao.org",
    ]

    # External APIs
    GOOGLE_PLACES_API_KEY: str = ""  # Optional — enables real venue search
    SERPAPI_KEY: str = ""            # Google Events via SerpAPI (set in Railway env vars)
    EVENTBRITE_TOKEN: str = ""       # Eventbrite public API token (set in Railway env vars)
    TICKETMASTER_API_KEY: str = ""   # Ticketmaster Discovery API (optional)

    # Feedback screenshots — local dev fallback or S3/R2-compatible object storage
    FEEDBACK_SCREENSHOT_STORAGE_BACKEND: str = "local"  # local | s3
    FEEDBACK_SCREENSHOT_LOCAL_DIR: str = "storage"
    FEEDBACK_SCREENSHOT_PUBLIC_BASE_URL: str = ""
    FEEDBACK_SCREENSHOT_MAX_BYTES: int = 5_000_000
    FEEDBACK_SCREENSHOT_S3_BUCKET: str = ""
    FEEDBACK_SCREENSHOT_S3_REGION: str = "auto"
    FEEDBACK_SCREENSHOT_S3_ENDPOINT_URL: str = ""
    FEEDBACK_SCREENSHOT_S3_ACCESS_KEY_ID: str = ""
    FEEDBACK_SCREENSHOT_S3_SECRET_ACCESS_KEY: str = ""
    FEEDBACK_SCREENSHOT_EPHEMERAL_DELETE: bool = True
    FEEDBACK_SCREENSHOT_AI_ANALYSIS_ENABLED: bool = True
    FEEDBACK_SCREENSHOT_AI_MODEL: str = "gpt-4o-mini"

    # Google OAuth (for Sign In with Google + Drive integration)
    # Setup: console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0 Client ID
    # Authorized redirect URI: https://connectome-api-production.up.railway.app/api/auth/google/callback
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "https://connectome-api-production.up.railway.app/api/auth/google/callback"
    FACEBOOK_APP_ID: str = ""
    APPLE_CLIENT_ID: str = ""
    IOS_BUNDLE_ID: str = ""


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
    ADMIN_TOKEN: str = ""
    ADMIN_SECRET: str = ""

    # Webhooks / background worker auth
    GITHUB_WEBHOOK_SECRET: str = ""
    ORA_JWT_TOKEN: str = ""
    CONNECTOME_WORKER_JWT: str = ""

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
    def has_ticketmaster(self) -> bool:
        return bool(self.TICKETMASTER_API_KEY)

    @property
    def is_production(self) -> bool:
        if self.APP_ENV.lower() == "production":
            return True
        # Fail safe: hosted cloud runtimes should be treated as production even
        # if APP_ENV was accidentally omitted.
        cloud_indicators = (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
            "RAILWAY_DEPLOYMENT_ID",
            "RAILWAY_STATIC_URL",
            "RENDER",
            "RENDER_SERVICE_ID",
            "FLY_APP_NAME",
            "K_SERVICE",  # Google Cloud Run
        )
        return any(os.getenv(key) for key in cloud_indicators)

    def validate_production_safety(self) -> None:
        """Fail fast on unsafe production configuration.

        Localhost/dev defaults are useful for development, but production should
        only boot with explicit environment-backed values for secrets and
        infrastructure URLs.
        """
        if not self.is_production:
            return

        errors = []
        if self.SECRET_KEY == "dev-secret-key-change-in-production-32chars!!" or len(self.SECRET_KEY) < 32:
            errors.append("SECRET_KEY must be a non-default value of at least 32 characters")
        if "localhost" in self.DATABASE_URL or "127.0.0.1" in self.DATABASE_URL:
            errors.append("DATABASE_URL must not point at localhost in production")
        if "localhost" in self.REDIS_URL or "127.0.0.1" in self.REDIS_URL:
            errors.append("REDIS_URL must not point at localhost in production")
        if any(origin == "*" for origin in self.CORS_ORIGINS):
            errors.append("CORS_ORIGINS must not contain '*' in production")
        admin_secret = self.ADMIN_TOKEN or self.ADMIN_SECRET
        if not admin_secret or admin_secret == "connectome-admin-secret" or len(admin_secret) < 32:
            errors.append("ADMIN_TOKEN or ADMIN_SECRET must be configured with a non-default value of at least 32 characters")
        if not self.GITHUB_WEBHOOK_SECRET:
            errors.append("GITHUB_WEBHOOK_SECRET must be configured in production")
        if self.STRIPE_SECRET_KEY and not self.STRIPE_WEBHOOK_SECRET:
            errors.append("STRIPE_WEBHOOK_SECRET must be configured when STRIPE_SECRET_KEY is set in production")
        if not (self.ORA_JWT_TOKEN or self.CONNECTOME_WORKER_JWT):
            errors.append("ORA_JWT_TOKEN or CONNECTOME_WORKER_JWT must be configured for production workers")
        if self.FEEDBACK_SCREENSHOT_STORAGE_BACKEND.lower().strip() == "local":
            errors.append("FEEDBACK_SCREENSHOT_STORAGE_BACKEND must not be 'local' in production")

        if errors:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))

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
