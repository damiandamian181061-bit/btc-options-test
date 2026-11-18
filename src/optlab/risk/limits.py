"""The risk limits of ``configs/risk/<name>.yaml`` (the brief's "Sizing and limits" table)."""

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from optlab.risk.margin import MarginRules


class RiskConfig(BaseModel):
    """Limits for the risk gateway. ``None`` switches a limit off (for benchmarks)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    currency: str
    starting_equity: Decimal
    max_loss_of_equity: Decimal | None
    max_open_groups: int | None
    max_open_per_expiry: int | None
    max_total_loss_of_equity: Decimal | None
    max_delta_of_equity: Decimal | None
    max_vega_of_equity: Decimal | None
    daily_loss_limit: Decimal | None
    kill_switch_drawdown: Decimal | None
    margin: MarginRules


def load_risk_config(config_dir: Path, name: str) -> RiskConfig:
    path = config_dir / "risk" / f"{name}.yaml"
    return RiskConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
