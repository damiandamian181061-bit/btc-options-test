# configs

YAML configs loaded into Pydantic models:

- `fees/`: exchange fee schedules, each with its source page and effective-from date
  (`deribit.yaml`, confirmed by Damian on 15 Nov 2026).
- `risk/`: risk limits for the risk gateway (the brief's sizing and limits table);
  `rough.yaml` is the ROUGH Phase 2B subset.
- `strategies/`: strategy rules (the brief's trading rules v1); `rough.yaml` is the ROUGH
  Phase 2B rule.
- `backtests/`: backtest run configs.
- `data_quality.yaml`: thresholds of the hourly data checks (confirmed 15 Nov 2026).
