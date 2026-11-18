"""Backtest configuration: a YAML file in ``configs/backtests`` loaded into Pydantic."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from optlab.domain.clock import datetime_to_ms


class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class BacktestConfig(BaseModel):
    """One backtest. Saved whole with every run (``backtest_runs.config``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    book: Literal["linear"] = "linear"  # BTC_USDC; the inverse fallback is not backtested yet
    start: datetime  # inclusive, UTC
    end: datetime  # exclusive, UTC
    strategy: StrategyConfig
    risk: str = "v1"  # configs/risk/<risk>.yaml
    fee_multiplier: Decimal = Decimal(1)  # cost sensitivity (Phase 5: 0.5 to 3)
    seed: int = 0
    skip_flagged_minutes: bool = True  # no decisions in minutes the data checks marked to skip

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start and end must carry a time zone (UTC)")
        return value

    @property
    def start_ms(self) -> int:
        return datetime_to_ms(self.start)

    @property
    def end_ms(self) -> int:
        return datetime_to_ms(self.end)


def load_backtest_config(path: Path) -> BacktestConfig:
    return BacktestConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
