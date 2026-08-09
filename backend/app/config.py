from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    content_bot_data_dir: Path = Path("data")
    content_bot_database_url: str = "sqlite:///data/content-bot.db"
    mongodb_uri: str | None = None
    mongodb_database: str = "content_bot"
    # Optional comma-separated DNS servers used only for mongodb+srv lookups.
    # This helps when Windows exposes an unreachable resolver from a disconnected adapter.
    mongodb_dns_servers: str = ""
    content_bot_max_video_size_bytes: int = 524_288_000
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
    content_bot_host: str = "127.0.0.1"
    content_bot_port: int = 8000
    content_bot_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    youtube_api_key: str | None = None
    youtube_region_code: str = "VN"
    youtube_relevance_language: str = "vi"
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "ContentBot/0.1 (local research tool)"
    x_bearer_token: str | None = None
    mediacrawler_command: str | None = None
    mediacrawler_profile_dir: Path = Path("data/browser-profile")
    mediacrawler_timeout_seconds: int = 600
    content_bot_coccoc_executable_path: Path = Path(
        r"C:\Program Files\CocCoc\Browser\Application\browser.exe"
    )
    web_feed_urls: str = ""

    @property
    def data_dir(self) -> Path:
        return self.content_bot_data_dir.resolve()


settings = Settings()
