"""Platform settings, loaded from the environment and `.env` (prefix `TRADEBOT_`)."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Typed runtime configuration. Every key is optional and has a default."""

    model_config = SettingsConfigDict(
        env_prefix="TRADEBOT_",
        env_file=".env",
        extra="ignore",
    )

    db_path: Path = Path("./tradebot.db")
    cache_dir: Path = Path("~/.tradebot/cache")  # same default as data.config.DataConfig
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_url: str = "http://127.0.0.1:8000"
    log_level: LogLevel = "INFO"
    log_dir: Path = Path("./logs")
    max_catchup_bars: int = Field(default=400, ge=1)
    equity_close_delay_min: int = 30
    checkpoint_keep: int = Field(default=30, ge=1)
    # Alpaca market data keys (data only, never trading). Read from the un-prefixed
    # names in `.env`; held as SecretStr so they never show up in repr or dumps.
    alpaca_key_id: SecretStr | None = Field(default=None, validation_alias="APCA_API_KEY_ID")
    alpaca_secret_key: SecretStr | None = Field(
        default=None, validation_alias="APCA_API_SECRET_KEY"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        # Accept "info" as well as "INFO"; anything else still fails the Literal check.
        return value.upper() if isinstance(value, str) else value

    @field_validator("cache_dir", "db_path", "log_dir", mode="after")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        return value.expanduser()
