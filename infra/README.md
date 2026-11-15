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
