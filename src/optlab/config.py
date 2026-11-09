"""Runtime settings, read from environment variables and an optional ``.env`` file.

The platform is paper-only. Building ``Settings`` fails with a ``pydantic.ValidationError`` when
``LIVE_TRADING`` is true (or anything that is not a clear false), so a program that loads its
settings at start-up cannot run in live mode. ``TRADING_MODE`` accepts only ``paper`` and
``DERIBIT_ENV`` only ``public`` or ``test``. Settings are frozen, so they cannot be flipped once
loaded.

Field names map to the upper-case variables in ``.env.example``. Secrets are ``SecretStr``, so
they never appear in a repr, a log line or an error message. Empty values count as unset.
"""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_TRADING_REFUSED = (
    "LIVE_TRADING=true is refused: btc-options-lab is paper-only and will not start in live mode. "
    "Set LIVE_TRADING=false."
)


class Settings(BaseSettings):
    """All runtime configuration for btc-options-lab."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    # Safety switches.
    trading_mode: Literal["paper"] = "paper"
    live_trading: bool = False
    # public: Deribit public market data, no key. test: Deribit testnet (Phase 8).
    deribit_env: Literal["public", "test"] = "public"

    # Database (Phase 1). The URL carries a password.
    database_url: SecretStr | None = None

    # Cloudflare R2 nightly backups (Phase 2). The endpoint embeds the Cloudflare account ID,
    # which stays out of the repo and the logs.
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_endpoint: SecretStr | None = None
    r2_bucket: str | None = None

    # healthchecks.io ping URLs: anyone holding one can mark a check healthy.
    hc_ping_url_recorder: SecretStr | None = None
    hc_ping_url_backup: SecretStr | None = None
    hc_ping_url_disk: SecretStr | None = None

    # Cloudflare Tunnel for the public dashboard (Phase 6).
    cloudflare_tunnel_token: SecretStr | None = None

    # Deribit testnet credentials (Phase 8). No live keys exist anywhere.
    deribit_testnet_client_id: SecretStr | None = None
    deribit_testnet_client_secret: SecretStr | None = None

    # Optional third parties.
    jev_api_key: SecretStr | None = None
    tardis_api_key: SecretStr | None = None

    @field_validator("live_trading")
    @classmethod
    def _refuse_live_trading(cls, value: bool) -> bool:
        if value:
            raise ValueError(LIVE_TRADING_REFUSED)
        return value
