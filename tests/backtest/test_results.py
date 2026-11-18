"""Run records, the results tables and MLflow logging."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from optlab.market_data.view import ParquetSource
from optlab.simulator.config import StrategyConfig, load_backtest_config
from optlab.simulator.engine import Backtest
from optlab.simulator.results import log_mlflow, save_run, summary
from optlab.simulator.runinfo import RunInfo, config_hash, dataset_hash, git_state, run_info
from tests.backtest.market import FakeMarket
from tests.backtest.test_engine import CONFIGS, config
from tests.conftest import REPO_ROOT


def smoke_run() -> tuple[RunInfo, object]:
    source = FakeMarket(step_minutes=30).build()
    cfg = config()
    result = Backtest(cfg, source, config_dir=CONFIGS).run()
    return run_info(cfg, source, REPO_ROOT), result


def test_the_run_record_names_config_commit_dataset_and_seed() -> None:
    source = FakeMarket(step_minutes=30).build()
    cfg = config(seed=5)
    info = run_info(cfg, source, REPO_ROOT)
    commit, _ = git_state(REPO_ROOT)
    assert info.git_commit == commit != "unknown"
    assert info.seed == 5
    assert info.config_hash == config_hash(cfg)
    assert config_hash(config(seed=6)) != info.config_hash
    assert info.dataset_hash == dataset_hash(
        {date.fromisoformat(d): v for d, v in info.dataset_versions.items()}
    )
    other = FakeMarket(step_minutes=30, half_spread=11.0).build()
    assert run_info(cfg, other, REPO_ROOT).dataset_hash != info.dataset_hash
    assert info.run_id.endswith(f"-test-{info.config_hash[:6]}")


def test_parquet_dataset_versions_come_from_the_manifests(tmp_path: Path) -> None:
    root = tmp_path / "exports" / "v1" / "manifests"
    root.mkdir(parents=True)
    for day, version in (("2026-11-14", "aa"), ("2026-11-15", "bb"), ("2026-11-21", "cc")):
        (root / f"{day}.json").write_text(json.dumps({"dataset_version": version}))
    source = ParquetSource.from_data_dir(tmp_path)
    got = source.dataset_versions(date(2026, 11, 14), date(2026, 11, 20))
    assert got == {date(2026, 11, 14): "aa", date(2026, 11, 15): "bb"}


def test_the_backtest_configs_load() -> None:
    for name in ("smoke", "exercise"):
        cfg = load_backtest_config(REPO_ROOT / "configs" / "backtests" / f"{name}.yaml")
        assert (cfg.start_ms, cfg.end_ms) == (1_794_614_400_000, 1_795_219_200_000)
    smoke = load_backtest_config(REPO_ROOT / "configs" / "backtests" / "smoke.yaml")
    assert smoke.strategy == StrategyConfig(name="smoke", params={"amount": "0.1", "min_days": 6})


@pytest.mark.db
def test_results_are_saved_in_the_results_tables(database_url: str) -> None:
    info, result = smoke_run()
    save_run(database_url, result, info, mlflow_run_id="m1")  # type: ignore[arg-type]
    with psycopg.connect(database_url) as conn:
        run = conn.execute(
            "SELECT result_hash, final_equity, seed, mlflow_run_id, git_commit FROM backtest_runs"
        ).fetchone()
        assert run == (
            result.result_hash,  # type: ignore[attr-defined]
            result.final.equity,  # type: ignore[attr-defined]
            7,
            "m1",
            info.git_commit,
        )
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # type: ignore[index]  # noqa: S608
            for t in ("backtest_orders", "backtest_fills", "backtest_positions",
                      "backtest_equity", "backtest_groups")
        }  # fmt: skip
        assert counts == {
            "backtest_orders": len(result.orders),  # type: ignore[attr-defined]
            "backtest_fills": 2,
            "backtest_positions": 2,
            "backtest_equity": len(result.equity),  # type: ignore[attr-defined]
            "backtest_groups": 1,
        }
        fee = conn.execute("SELECT fee FROM backtest_fills WHERE kind = 'trade'").fetchone()
        assert fee == (result.fills[0].fee,)  # type: ignore[attr-defined]


@pytest.mark.db
def test_risk_events_are_saved_with_their_run(database_url: str) -> None:
    from optlab.risk.gateway import RiskEvent

    info, result = smoke_run()
    event = RiskEvent(1, "open groups", "rejected", "test", "t-0", "g2", Decimal(6), Decimal(5))
    result.risk_events.append(event)  # type: ignore[attr-defined]
    save_run(database_url, result, replace(info, run_id="r2"))  # type: ignore[arg-type]
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT source, run_id, rule, value, limit_value FROM risk_events")
        assert row.fetchall() == [("backtest", "r2", "open groups", Decimal(6), Decimal(5))]


def test_mlflow_logs_params_metrics_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mlflow

    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    info, result = smoke_run()
    run_id = log_mlflow(result, info, REPO_ROOT / "configs" / "backtests" / "smoke.yaml")  # type: ignore[arg-type]
    run = mlflow.get_run(run_id)
    assert run.data.tags["result_hash"] == result.result_hash  # type: ignore[attr-defined]
    assert run.data.tags["dataset_hash"] == info.dataset_hash
    assert run.data.params["seed"] == "7"
    s = summary(result)  # type: ignore[arg-type]
    assert run.data.metrics["final_equity"] == float(s["final_equity"])
