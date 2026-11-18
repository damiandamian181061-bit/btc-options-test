# infra

- `server-setup.md`: how to create and harden the server from scratch (the reboot policy is in
  section 6), how to run the recorder on it (section 14), the data platform (section 15) and
  the rough paper service (section 16).
- `compose.yaml`: the services (`db`, `recorder`, `health`, `jobs`, `dashboard`, and the
  ROUGH `paper` service). Run it through `dc`.
- `restore-test`, `restore-test.compose.yaml`: the restore test, in its own throwaway Compose
  project; `systemd/` runs it monthly.
- `dc`: `docker compose` with the repo's `.env`; refuses `config`, which would print secrets.
- `docker/Dockerfile`: the `optlab` image the services run.
- `db/entrypoint.sh`: starts TimescaleDB with the user, password and database from
  `DATABASE_URL`.
- `alembic.ini`, `migrations/`: the database schema. Back up before every migration on the
  server, and restart the recorder on the new image after it.
- `apt/52optlab-reboot`: the unattended-upgrades reboot policy (04:00 UTC, only when needed).
- `sql/`: the Phase 1 gate queries (`gap_check.sql`, `volume.sql`). The Phase 2 gap report is
  `optlab-data gap-report`.

Never write the server's IP address, the R2 endpoint or the Cloudflare account ID here: use
placeholders such as `SERVER_IP`.

## Database tests on the laptop

The schema and store tests need a throwaway TimescaleDB; without one they are skipped (CI always
runs them). Start one, bound to localhost only, and point the tests at it:

```bash
docker run -d --name optlab-testdb -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=optlab_test -e POSTGRES_PASSWORD=optlab_test -e POSTGRES_DB=optlab_test \
  -e NO_TS_TUNE=true timescale/timescaledb:2.30.2-pg17
OPTLAB_TEST_DATABASE_URL=postgresql://optlab_test:optlab_test@127.0.0.1:55432/optlab_test uv run pytest
```

The backup and restore tests also need `rclone` on the PATH (or `OPTLAB_RCLONE` naming the
binary); without it they are skipped (CI always runs them).

## Backtests on the laptop (Phase 4)

**rclone 1.75.2**, the same pinned binary as the server image and CI, copied out of the
official image (installed on 18 Nov 2026 into `~/.local/bin`):

```bash
img='rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5'
id=$(docker create "$img") && docker cp "$id":/usr/local/bin/rclone ~/.local/bin/rclone && docker rm "$id"
```

**The read-only R2 token** goes in the laptop's `.env` (git-ignored; Damian types it with nano,
nothing reads or prints it): `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` and
`R2_BUCKET`. Never the server's read-write token. Then `uv run optlab-data sync --list` and
`uv run optlab-data sync --from DAY --to DAY` pull exported days into `data/exports/v1`.

**The results database**, a local TimescaleDB that keeps the backtest results, on localhost only
and with its own volume (the password is a local, made-up one, as for the test database):

```bash
docker run -d --name optlab-results -p 127.0.0.1:55433:5432 -v optlab-results:/var/lib/postgresql/data \
  -e POSTGRES_USER=optlab -e POSTGRES_PASSWORD=optlab_local -e POSTGRES_DB=optlab_results \
  -e NO_TS_TUNE=true timescale/timescaledb:2.30.2-pg17
export OPTLAB_RESULTS_DATABASE_URL=postgresql://optlab:optlab_local@127.0.0.1:55433/optlab_results
DATABASE_URL="$OPTLAB_RESULTS_DATABASE_URL" uv run alembic -c infra/alembic.ini upgrade head
```

Then `uv run optlab-backtest run configs/backtests/smoke.yaml` (`docs/backtest.md`).
