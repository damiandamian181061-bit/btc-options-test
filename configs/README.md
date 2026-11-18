# configs

YAML configs loaded into Pydantic models:

- `fees/`: exchange fee schedules, each with its source page and effective-from date
  (`deribit.yaml`, confirmed by Damian on 15 Nov 2026; re-read 18 Nov 2026, unchanged; it also
  holds the perpetual fees and Deribit's funding rules for the backtests).
- `risk/`: risk limits for the risk gateway (the brief's sizing and limits table): `v1.yaml`
  (Phase 4, with Deribit Standard Margin), `exercise.yaml` (the gate's engine-exercise runs
  only), and `rough.yaml`, the ROUGH Phase 2B subset.
- `strategies/`: strategy rules (the brief's trading rules v1); `rough.yaml` is the ROUGH
  Phase 2B rule.
- `backtests/`: backtest run configs (`docs/backtest.md`): `smoke.yaml` and `exercise.yaml`
  for the Phase 4 gate.
- `data_quality.yaml`: thresholds of the hourly data checks (confirmed 15 Nov 2026).
