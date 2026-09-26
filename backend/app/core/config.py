from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./healthpilot.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "models/text-embedding-004"
    mapping_confidence_threshold: float = 0.75
    # Comma-separated string, not list[str]: pydantic-settings JSON-decodes
    # env vars for complex/list-typed fields, which breaks on a plain value
    # like "*" or "https://a.com,https://b.com". Split it ourselves instead.
    cors_origins: str = "*"
    max_upload_mb: int = 15

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
