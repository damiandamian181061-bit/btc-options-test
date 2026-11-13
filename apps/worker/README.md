# apps/worker

Long-running services deployed on the server with Docker Compose (`infra/compose.yaml`). They
are thin entry points: the logic lives in `src/optlab`.

- `optlab-recorder` (Phase 1, `optlab.market_data.service:recorder_main`): records Deribit
  public market data into TimescaleDB and the raw Parquet stream.
- `optlab-health` (Phase 1, `optlab.market_data.service:health_main`): healthchecks.io pings for
  the recorder and the disk.
- Later: the nightly backup and data checks (Phase 2) and the paper-trading service (Phases 2B
  and 8).

Both load `optlab.config.Settings` first, so they refuse to start when `LIVE_TRADING` is true.
Non-secret tuning comes from `OPTLAB_*` variables (see `ServiceSettings` in
`src/optlab/market_data/service.py`).
