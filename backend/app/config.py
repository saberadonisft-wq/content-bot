import logging
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("content_bot.config")

_LEGACY_GEMINI_ENV = {
    "CONTENT_BOT_GEMINI_CLI_ENABLED": "CONTENT_BOT_GEMINI_ENABLED",
    "CONTENT_BOT_GEMINI_CLI_MODEL": "CONTENT_BOT_GEMINI_MODEL",
    "CONTENT_BOT_GEMINI_CLI_TIMEOUT_SECONDS": "CONTENT_BOT_GEMINI_TIMEOUT_SECONDS",
    "CONTENT_BOT_GEMINI_CLI_CHUNK_SECONDS": "CONTENT_BOT_GEMINI_CHUNK_SECONDS",
    "CONTENT_BOT_GEMINI_CLI_MAX_INPUT_MB": "CONTENT_BOT_GEMINI_MAX_INPUT_MB",
    "CONTENT_BOT_GEMINI_CLI_MAX_RETRIES": "CONTENT_BOT_GEMINI_MAX_RETRIES",
    "CONTENT_BOT_GEMINI_CLI_RETRY_BASE_SECONDS": "CONTENT_BOT_GEMINI_RETRY_BASE_SECONDS",
    "CONTENT_BOT_GEMINI_CLI_API_KEY": "GEMINI_API_KEY",
    "CONTENT_BOT_GEMINI_CLI_PATH": "ignored",
}
_legacy_gemini_warning_emitted = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    content_bot_data_dir: Path = Path("data")
    content_bot_storage_backend: str = "sqlite"
    content_bot_sqlite_path: Path | None = None
    mongodb_uri: str | None = None
    mongodb_database: str = "content_bot"
    # Optional comma-separated DNS servers used only for mongodb+srv lookups.
    # This helps when Windows exposes an unreachable resolver from a disconnected adapter.
    mongodb_dns_servers: str = ""
    # Keep uploads practical for long-form source videos while still preventing
    # accidental unbounded disk usage. Override with CONTENT_BOT_MAX_VIDEO_SIZE_BYTES.
    content_bot_max_video_size_bytes: int = 2_147_483_648
    content_bot_max_overlay_size_bytes: int = 10_485_760
    content_bot_media_probe_timeout_seconds: int = 120
    content_bot_thumbnail_timeout_seconds: int = 90
    content_bot_subtitle_job_concurrency: int = 1
    content_bot_alignment_engine: str = "energy"
    content_bot_alignment_whisper_model: str = "small"
    content_bot_alignment_whisper_device: str = "auto"
    content_bot_alignment_whisper_compute_type: str = "auto"
    content_bot_alignment_whisper_allow_download: bool = False
    content_bot_alignment_cpu_threads: int = 4
    content_bot_alignment_extract_timeout_seconds: int = 90
    content_bot_subtitle_render_timeout_seconds: int = 7200
    content_bot_gemini_enabled: bool = Field(
        True,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_ENABLED", "CONTENT_BOT_GEMINI_CLI_ENABLED"
        ),
    )
    content_bot_gemini_model: str = Field(
        "gemini-3.6-flash",
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_MODEL", "CONTENT_BOT_GEMINI_CLI_MODEL"
        ),
    )
    gemini_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GEMINI_API_KEY", "CONTENT_BOT_GEMINI_CLI_API_KEY"
        ),
    )
    content_bot_gemini_timeout_seconds: int = Field(
        1800,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_TIMEOUT_SECONDS",
            "CONTENT_BOT_GEMINI_CLI_TIMEOUT_SECONDS",
        ),
    )
    content_bot_gemini_chunk_seconds: int = Field(
        180,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_CHUNK_SECONDS", "CONTENT_BOT_GEMINI_CLI_CHUNK_SECONDS"
        ),
    )
    content_bot_gemini_max_input_mb: int = Field(
        19,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_MAX_INPUT_MB", "CONTENT_BOT_GEMINI_CLI_MAX_INPUT_MB"
        ),
    )
    content_bot_gemini_max_retries: int = Field(
        5,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_MAX_RETRIES", "CONTENT_BOT_GEMINI_CLI_MAX_RETRIES"
        ),
    )
    content_bot_gemini_retry_base_seconds: float = Field(
        30.0,
        validation_alias=AliasChoices(
            "CONTENT_BOT_GEMINI_RETRY_BASE_SECONDS",
            "CONTENT_BOT_GEMINI_CLI_RETRY_BASE_SECONDS",
        ),
    )
    content_bot_host: str = "127.0.0.1"
    content_bot_port: int = 8000
    content_bot_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    content_bot_frontend_url: str = "http://127.0.0.1:5173/"
    content_bot_crawler_retention_days: int = 90
    content_bot_crawler_retention_interval_hours: int = 24
    content_bot_auth_enabled: bool = False
    content_bot_auth_server_url: str = ""
    content_bot_auth_public_key: str = ""
    youtube_api_key: str | None = None
    youtube_region_code: str = "VN"
    youtube_relevance_language: str = "vi"
    youtube_search_request_budget: int = 10
    youtube_general_request_budget: int = 25
    youtube_comment_request_budget: int = 25
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "ContentBot/0.1 (local research tool)"
    reddit_comment_request_budget: int = 20
    steam_max_discovered_apps: int = 3
    steam_discovery_request_budget: int = 5
    steam_review_request_budget: int = 20
    steam_review_filter: str = "recent"
    steam_review_language: str = "all"
    steam_purchase_type: str = "all"
    bluesky_request_budget: int = 20
    mastodon_instances: str = "mastodon.social,mastodon.gamedev.place,dice.camp"
    mastodon_request_budget: int = 30
    x_bearer_token: str | None = None
    meta_graph_api_version: str = ""
    meta_access_token: str | None = None
    instagram_professional_user_id: str = ""
    facebook_page_access_token: str | None = None
    facebook_page_id: str = ""
    facebook_page_username: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str | None = None
    tiktok_redirect_uri: str = ""
    tiktok_user_access_token: str | None = None
    tiktok_user_refresh_token: str | None = None
    tiktok_open_id: str = ""
    tiktok_authorized_username: str = ""
    tiktok_granted_scopes: str = ""
    content_bot_cbce_enabled: bool = False
    content_bot_cbce_profile_root: Path = Path("data/browser-profiles-v2")
    content_bot_cbce_event_queue_size: int = 100
    content_bot_cbce_cleanup_timeout_seconds: float = 10
    content_bot_cbce_provider_overrides: str = "{}"
    content_bot_cbce_browser_executable_path: Path | None = None
    content_bot_cbce_contract_root: Path = Path("data/crawler-contracts-v1")
    # MediaCrawler-derived modules are enabled only for this project's
    # explicitly non-commercial learning/research deployment.  A commercial
    # build must set this false and fail closed until those modules are
    # replaced or separately relicensed.
    content_bot_licensed_reuse_noncommercial_only: bool = True
    mediacrawler_command: str | None = None
    mediacrawler_profile_dir: Path = Path("data/browser-profile")
    mediacrawler_timeout_seconds: int = 600
    content_bot_coccoc_executable_path: Path = Path(
        r"C:\Program Files\CocCoc\Browser\Application\browser.exe"
    )
    web_feed_urls: str = ""

    @model_validator(mode="before")
    @classmethod
    def warn_for_legacy_gemini_environment(cls, values: object) -> object:
        global _legacy_gemini_warning_emitted
        if not _legacy_gemini_warning_emitted and isinstance(values, dict):
            legacy_names = {name.lower() for name in _LEGACY_GEMINI_ENV}
            if any(str(key).lower() in legacy_names for key in values):
                logger.warning(
                    "Legacy CONTENT_BOT_GEMINI_CLI_* environment names are deprecated; "
                    "use the REST Gemini configuration names instead."
                )
                _legacy_gemini_warning_emitted = True
        return values

    @property
    def data_dir(self) -> Path:
        if self.content_bot_data_dir.is_absolute():
            return self.content_bot_data_dir.resolve()
        return (PROJECT_ROOT / self.content_bot_data_dir).resolve()

    @property
    def sqlite_path(self) -> Path:
        if self.content_bot_sqlite_path is not None:
            if self.content_bot_sqlite_path.is_absolute():
                return self.content_bot_sqlite_path.resolve()
            return (PROJECT_ROOT / self.content_bot_sqlite_path).resolve()
        return self.data_dir / "content-bot.db"


settings = Settings()
