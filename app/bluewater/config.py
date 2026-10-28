import os

VERSION = "3.3.0"   # shown in the sidebar, on the start page and at /api/health


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg://bluewater:bluewater@localhost:5432/bluewater")
    secret_key: str = os.getenv("SECRET_KEY", "change-me-in-production")
    admin_user: str = os.getenv("ADMIN_USER", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin123")
    api_key: str = os.getenv("API_KEY", "")
    seed_demo: bool = os.getenv("SEED_DEMO", "true").lower() in ("1", "true", "yes")


settings = Settings()
