"""Settings must refuse to start in live-trading mode, and only paper modes exist."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from optlab.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
FIELD_ENV_NAMES = {name.upper() for name in Settings.model_fields}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of every test."""
    for name in FIELD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def load(env_file: Path | None = None) -> Settings:
    return Settings(_env_file=env_file)


def test_defaults_are_paper_only() -> None:
    settings = load()
    assert settings.live_trading is False
    assert settings.trading_mode == "paper"
    assert settings.deribit_env == "public"


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
def test_live_trading_true_in_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LIVE_TRADING", value)
    with pytest.raises(ValidationError, match="paper-only and will not start"):
        load()


def test_live_trading_true_in_env_file_is_refused(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_MODE=paper\nLIVE_TRADING=true\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="paper-only and will not start"):
        load(env_file)


@pytest.mark.parametrize("value", ["maybe", "live", "2"])
def test_unclear_live_trading_value_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LIVE_TRADING", value)
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_live_trading_false_is_accepted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LIVE_TRADING", value)
    assert load().live_trading is False


def test_live_trading_cannot_be_switched_on_after_loading() -> None:
    settings = load()
    with pytest.raises(ValidationError):
        settings.live_trading = True  # type: ignore[misc]


def test_only_paper_trading_mode_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize("value", ["prod", "production", "www"])
def test_production_deribit_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DERIBIT_ENV", value)
    with pytest.raises(ValidationError):
        load()


def test_env_example_lists_exactly_the_settings_fields() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    names = {line.split("=", 1)[0] for line in lines if line and not line.startswith("#")}
    assert names == FIELD_ENV_NAMES


def test_env_example_loads_with_safe_defaults_and_no_values() -> None:
    settings = load(ENV_EXAMPLE)
    assert settings.live_trading is False
    assert settings.trading_mode == "paper"
    assert settings.deribit_env == "public"
    unset = {"trading_mode", "live_trading", "deribit_env"}
    for name in Settings.model_fields.keys() - unset:
        assert getattr(settings, name) is None, f"{name.upper()} should be blank in .env.example"


def test_secrets_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "placeholder-value")
    settings = load()
    assert settings.r2_secret_access_key is not None
    assert settings.r2_secret_access_key.get_secret_value() == "placeholder-value"
    assert "placeholder-value" not in repr(settings)
    assert "placeholder-value" not in str(settings)
