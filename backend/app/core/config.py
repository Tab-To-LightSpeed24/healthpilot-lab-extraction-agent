from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./healthpilot.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    mapping_confidence_threshold: float = 0.75
    # LLM use is optional: when disabled, unreachable, over budget, or failing,
    # extraction falls back to the local rule-based parser (see
    # app/services/extraction/fallback.py) and results are flagged as such.
    llm_enabled: bool = True
    # Hard cap on a single HTTP request to the LLM provider.
    llm_request_timeout_seconds: float = 25.0
    # Total wall-clock budget for one LLM call including retries; once spent,
    # the call is abandoned and the page is diverted to the fallback.
    llm_page_budget_seconds: float = 45.0
    # Fallback rows are rule-parsed, not model-scored: capped below the
    # 0.7 low-confidence threshold used by quality.py so they surface for
    # review instead of looking as trustworthy as LLM output.
    fallback_extraction_confidence: float = 0.65
    # Local OCR for scanned/image input (fallback path). Empty = auto-detect
    # Tesseract on PATH or in its usual Windows install folders.
    ocr_enabled: bool = True
    tesseract_cmd: str = ""
    # Wall-clock cap for one Tesseract run on one page; a hung or pathological
    # page is abandoned rather than blocking the worker.
    ocr_page_timeout_seconds: float = 40.0
    # Pages OCR'd in the background while earlier pages are mapped/saved.
    # Default 1 on purpose: measured inside a 512MB container, one OCR of a
    # large page peaks ~424MB, so two concurrent OCRs would be OOM-killed on
    # Render's free tier. Raise only on a bigger instance.
    ocr_max_workers: int = 1
    # Whole-document cap for local (fallback) extraction: max(this, 15s/page).
    fallback_document_budget_floor_seconds: float = 60.0
    fallback_seconds_per_page: float = 15.0
    # Comma-separated string, not list[str]: pydantic-settings JSON-decodes
    # env vars for complex/list-typed fields, which breaks on a plain value
    # like "*" or "https://a.com,https://b.com". Split it ourselves instead.
    cors_origins: str = "*"
    max_upload_mb: int = 15

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
