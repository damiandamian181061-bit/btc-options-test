"""``optlab-backtest``: run a backtest on the laptop from days synced from R2.

    optlab-backtest run configs/backtests/smoke.yaml        run, save, log to MLflow
    optlab-backtest run CONFIG --no-save --no-mlflow         run and print the summary only

Market data comes from ``data/exports/v1`` (``optlab-data sync``). Results go to the database
named by OPTLAB_RESULTS_DATABASE_URL (a local PostgreSQL, infra/README.md) and to MLflow.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from optlab.config import Settings
from optlab.market_data.view import ParquetSource
from optlab.simulator.config import load_backtest_config
from optlab.simulator.engine import Backtest
from optlab.simulator.results import RESULTS_URL_ENV, log_mlflow, save_run, summary
from optlab.simulator.runinfo import run_info


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="optlab-backtest", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run one backtest config")
    r.add_argument("config", type=Path)
    r.add_argument("--data-dir", type=Path, default=Path("data"))
    r.add_argument("--config-dir", type=Path, default=Path("configs"))
    r.add_argument("--no-save", action="store_true", help="don't write the results tables")
    r.add_argument("--no-mlflow", action="store_true", help="don't log to MLflow")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    Settings()  # refuses to start if LIVE_TRADING=true
    args = _parser().parse_args(argv)
    config = load_backtest_config(args.config)
    source = ParquetSource.from_data_dir(args.data_dir)
    info = run_info(config, source)
    if not info.dataset_versions:
        print(f"no synced days for {config.start:%Y-%m-%d} to {config.end:%Y-%m-%d} "
              f"under {args.data_dir} (run optlab-data sync)", file=sys.stderr)  # fmt: skip
        return 2
    result = Backtest(config, source, config_dir=args.config_dir).run()
    mlflow_id = None if args.no_mlflow else log_mlflow(result, info, args.config)
    if not args.no_save:
        url = os.environ.get(RESULTS_URL_ENV)
        if not url:
            print(f"{RESULTS_URL_ENV} is not set: results not saved (or pass --no-save)",
                  file=sys.stderr)  # fmt: skip
            return 2
        save_run(url, result, info, mlflow_run_id=mlflow_id)
    out = {"run_id": info.run_id, "git_commit": info.git_commit, "git_dirty": info.git_dirty,
           "dataset_hash": info.dataset_hash, "days": list(info.dataset_versions),
           **summary(result), "mlflow_run_id": mlflow_id}  # fmt: skip
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
