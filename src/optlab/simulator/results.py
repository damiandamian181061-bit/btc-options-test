"""Saving a run: the results tables (laptop database) and MLflow (laptop, 127.0.0.1 only).

The results database is a local PostgreSQL named by ``OPTLAB_RESULTS_DATABASE_URL`` (see
infra/README.md); its tables come from the same migrations as the server's (0005). MLflow logs
to ``mlruns/`` in the repo (git-ignored) unless ``MLFLOW_TRACKING_URI`` says otherwise; its UI
is started by hand, on 127.0.0.1 only (docs/backtest.md).
"""

import json
import os
import time
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import astuple
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.exc import SADeprecationWarning

from optlab.market_data import schema
from optlab.simulator.engine import BacktestResult
from optlab.simulator.runinfo import RunInfo

RESULTS_URL_ENV = "OPTLAB_RESULTS_DATABASE_URL"
DEFAULT_MLFLOW_URI = "sqlite:///mlruns/mlflow.db"


def _copy(cur: psycopg.Cursor[Any], table: str, columns: Sequence[str],
          rows: Iterable[Sequence[Any]]) -> None:  # fmt: skip
    query = sql.SQL("COPY {} (run_id, {}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
    )
    with cur.copy(query) as copy:
        for row in rows:
            copy.write_row(row)


def save_run(
    database_url: str, result: BacktestResult, info: RunInfo, *, mlflow_run_id: str | None = None
) -> None:
    """Write the run and every record into the results tables, in one transaction."""
    rid = info.run_id
    final = result.final
    schema.check_url(database_url, schema.BACKTEST_TABLES, "the backtest results")
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO backtest_runs (run_id, name, config, config_hash, git_commit, git_dirty,"
            " dataset_hash, dataset_versions, seed, start_ms, end_ms, minutes, started_ms,"
            " finished_ms, result_hash, final_equity, mlflow_run_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                rid, result.config.name, json.dumps(result.config.model_dump(mode="json")),
                info.config_hash, info.git_commit, info.git_dirty, info.dataset_hash,
                json.dumps(info.dataset_versions), info.seed, result.config.start_ms,
                result.config.end_ms, result.minutes, info.started_ms,
                time.time_ns() // 1_000_000, result.result_hash,
                None if final is None else final.equity, mlflow_run_id,
            ),
        )  # fmt: skip
        _copy(cur, "backtest_orders",
              ("ticket_id", "group_id", "ts_ms", "leg", "instrument", "direction", "intent",
               "requested", "filled", "status", "reason", "detail"),
              ((rid, *astuple(r)) for r in result.orders))  # fmt: skip
        _copy(cur, "backtest_fills",
              ("fill_id", "ticket_id", "group_id", "ts_ms", "instrument", "kind", "direction",
               "amount", "price", "fee", "index_price", "mid", "mark", "delivery_price"),
              ((rid, *astuple(r)) for r in result.fills))  # fmt: skip
        _copy(cur, "backtest_positions",
              ("seq", "ts_ms", "group_id", "instrument", "qty", "entry_value"),
              ((rid, i, *astuple(r)) for i, r in enumerate(result.positions)))  # fmt: skip
        _copy(cur, "backtest_equity",
              ("ts_ms", "equity", "liquidation_equity", "cash", "option_value",
               "hedge_unrealised", "option_pnl", "hedge_pnl", "fees", "funding", "open_groups",
               "max_loss", "initial_margin", "maintenance_margin", "delta", "vega",
               "stale_marks"),
              ((rid, *astuple(r)) for r in result.equity))  # fmt: skip
        _copy(cur, "backtest_groups",
              ("group_id", "opened_ms", "closed_ms", "thesis", "reason", "exit_reason",
               "realised", "fees"),
              ((rid, *astuple(r)) for r in result.groups))  # fmt: skip
        cur.executemany(
            "INSERT INTO risk_events (source, run_id, ts_ms, rule, action, detail, ticket_id,"
            " group_id, value, limit_value)"
            " VALUES ('backtest', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(rid, e.ts_ms, e.rule, e.action, e.detail, e.ticket_id, e.group_id, e.value, e.limit)
             for e in result.risk_events],
        )  # fmt: skip


def summary(result: BacktestResult) -> dict[str, Any]:
    final = result.final
    trades = sum(1 for f in result.fills if f.kind == "trade")
    out: dict[str, Any] = {
        "minutes": result.minutes,
        "groups": len(result.groups),
        "trade_fills": trades,
        "settlements": sum(1 for f in result.fills if f.kind == "settlement"),
        "rejected_tickets": len({o.ticket_id for o in result.orders if o.status == "rejected"}),
        "risk_events": len(result.risk_events),
        "result_hash": result.result_hash,
    }
    if final is not None:
        out.update(
            final_equity=str(final.equity),
            pnl=str(final.equity - result.capital),
            option_pnl=str(final.option_pnl),
            hedge_pnl=str(final.hedge_pnl),
            fees=str(final.fees),
            funding=str(final.funding),
            max_stale_marks=max(r.stale_marks for r in result.equity),
        )
    return out


def log_mlflow(result: BacktestResult, info: RunInfo, config_path: Path | None) -> str:
    """Log the run to MLflow (params, metrics, the config file); returns MLflow's run id."""
    # MLflow's SQL store still uses a loader SQLAlchemy 2.1 deprecates; harmless here.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SADeprecationWarning)
        return _log_mlflow(result, info, config_path)


def _log_mlflow(result: BacktestResult, info: RunInfo, config_path: Path | None) -> str:
    import mlflow  # laptop only (dependency group "research")

    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", DEFAULT_MLFLOW_URI))
    if mlflow.get_tracking_uri().startswith("sqlite:///mlruns/"):
        Path("mlruns").mkdir(exist_ok=True)
    mlflow.set_experiment(f"backtest-{result.config.strategy.name}")
    s = summary(result)
    with mlflow.start_run(run_name=info.run_id) as run:
        mlflow.set_tags({
            "run_id": info.run_id, "git_commit": info.git_commit,
            "git_dirty": str(info.git_dirty), "dataset_hash": info.dataset_hash,
            "result_hash": result.result_hash, "config_hash": info.config_hash,
        })  # fmt: skip
        mlflow.log_params({
            "name": result.config.name, "strategy": result.config.strategy.name,
            "risk": result.config.risk, "fee_multiplier": str(result.config.fee_multiplier),
            "seed": result.config.seed, "start": result.config.start.isoformat(),
            "end": result.config.end.isoformat(),
            **{f"param.{k}": str(v) for k, v in result.config.strategy.params.items()},
        })  # fmt: skip
        metrics = {k: float(v) for k, v in s.items() if k != "result_hash"}
        mlflow.log_metrics(metrics)
        mlflow.log_dict(s, "summary.json")
        mlflow.log_dict(info.dataset_versions, "dataset_versions.json")
        if config_path is not None:
            mlflow.log_artifact(str(config_path))
        return str(run.info.run_id)
