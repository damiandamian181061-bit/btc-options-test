# apps/worker

Long-running services deployed on the server with Docker Compose (`infra/compose.yaml`). They
are thin entry points: the logic lives in `src/optlab`.

- `optlab-recorder` (Phase 1, `optlab.market_data.service:recorder_main`): records Deribit
  public market data into TimescaleDB and the raw Parquet stream.
- `optlab-health` (Phase 1, `optlab.market_data.service:health_main`): healthchecks.io pings for
  the recorder and the disk.
- `optlab-data` (Phase 2, `optlab.market_data.jobs:main`): the hourly data checks and the
  nightly R2 backup (the `jobs` service), plus the gap report, restore test and laptop sync.
- `optlab-paper-rough` (Phase 2B, `optlab.simulator.rough_paper:main`): the ROUGH paper
  service (the `paper` service). It runs the rough rule on live recorded data with paper fills
  only and sends no orders anywhere (docs/rough-version.md). Phase 8 replaces it.

All of them load `optlab.config.Settings` first, so they refuse to start when `LIVE_TRADING` is true.
Non-secret tuning comes from `OPTLAB_*` variables (see `ServiceSettings` in
`src/optlab/market_data/service.py`).
