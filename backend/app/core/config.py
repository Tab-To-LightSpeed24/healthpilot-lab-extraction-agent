import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = os.environ.get(
        "DATABASE_URL", "sqlite:///./healthpilot.db"
    )
    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
    gemini_model: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_embedding_model: str = os.environ.get(
        "GEMINI_EMBEDDING_MODEL", "models/text-embedding-004"
    )
    mapping_confidence_threshold: float = float(
        os.environ.get("MAPPING_CONFIDENCE_THRESHOLD", "0.75")
    )
    cors_origins: list[str] = os.environ.get("CORS_ORIGINS", "*").split(",")
    max_upload_mb: int = int(os.environ.get("MAX_UPLOAD_MB", "15"))


settings = Settings()
