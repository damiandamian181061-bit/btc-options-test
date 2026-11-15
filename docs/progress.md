# Progress log

Read at the start of every session, updated at the end: what's done, what's next, open
questions. A phase ends only when its gate passes, with the evidence recorded here.

## Phases

| Phase | When | Gate before the next phase | Status |
| --- | --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | CI is green, Damian can SSH in as `deploy`, root login is refused, and `docker run hello-world` works on the server (backups and alerts configured) | **Passed** 10 Nov 2026 |
| 1 · Recorder live | Week 1 · days 2–4 | 24 h with no gap over 5 min in `option_quotes`; day-one volume measured and the raw-stream cap set from it | **Passed** 14 Nov 2026 (raw-stream cap chosen 14 Nov) |
| 2 · Data and checks | Week 1 · days 4–7 | Restore from R2 works; the gap report covers every recorded day; the liquidity report names the book | **Passed** 15 Nov 2026 |
| 2B · Rough end-to-end version | End of week 1 | Paper service runs 24 h and shows its trades; shortcut list exists (results don't count yet) | Open |
| 3 · Pricing and surface | Week 2 | IVs and Greeks match Deribit and finite differences within agreed tolerances; golden tests match btc-vol-models; live surface on the status page | Open |
| 4 · Backtest engine | Weeks 2–3 | Determinism, leakage and reconciliation tests pass on a real week; hand-checked smoke trade matches | Open |
| 5 · Strategies and results | Week 3 | Full report of v1 rules vs every benchmark on the locked test, after costs | Open |
| 6 · Streamlit dashboard | Weeks 3–4, in parallel | Public site loads every page on laptop and phone; nothing on it changes the system | Open |
| 7 · JEV challenger | Week 4 | JEV-versus-rules report on the same frozen folds, seeds and cost model | Open |
| 8 · Testnet paper trading | Weeks 3–4, runs a week | A week of paper decisions matches its replay, every difference explained | Open |
| 9 · Release v1.0 | End of week 4 | A fresh clone runs end to end with one command; history scan is clean | Open |
| 10 · Challenger models | Week 5, after v1.0 | Each challenger promoted or rejected on evidence | Open |

## Current phase

Phase 2 · Data and checks: **passed** on 15 November 2026. Phase 2B (rough end-to-end
version) has not started. Running on recorder-1: `db`, `recorder`, `health`, `jobs` (data
checks hourly, R2 backup nightly at 00:30 UTC) and `dashboard` (status page, server
localhost only); the restore test runs monthly from a systemd timer.

## Phase 2 · Data and checks: gate evidence (14–15 Nov 2026)

All three parts of the gate pass. Checked on recorder-1, finally at commit `27c0678` (the
deployed code).

**1. Restore from R2: PASS, twice.** `infra/restore-test DAY` runs in the throwaway project
`optlab-restore-test` (own TimescaleDB container, own volume, 1 GB memory limit, random password
never shown). It downloads the day from R2, checks every file and rebuilds a fresh database:

| Check | 13 Nov (14 Nov 21:35 UTC) | 14 Nov, full day (15 Nov 01:12–01:15 UTC) |
| --- | --- | --- |
| dataset version | `9895df467771eca3…` | `56a96323d91c0419…` |
| files downloaded from R2 | 31 files, 25.3 MB; SHA-256 all match | 33 files, 78.3 MB; SHA-256 all match |
| `option_quotes` | 715,682 / 715,682, checksum match | 2,325,985 / 2,325,985, match |
| `option_trades` | 3,502 / 3,502, match | 16,614 / 16,614, match |
| `underlying_ticks` | 10,724 / 10,724, match | 34,554 / 34,554, match |
| `instruments` | 1,624 / 1,624, match | 1,822 / 1,822, match |
| `delivery_prices` | 60 / 60, match | 2 / 2, match |
| `connection_events` | 36 / 36, match | 144 / 144, match |
| `data_quality`, `data_quality_runs`, `recorder_status` | 1, 8, 0: all match | 782, 24, 150: all match |
| cleanup | only the restore project's container, network and volume removed | the same; none left afterwards |

Rows are shown as in the manifest / restored. The checksum is computed by PostgreSQL over
every row's text in the live database at export time and again in the restored database, so a
match means the round trip database -> Parquet -> R2 -> database lost or changed nothing. The
second run used the fixed manifest read (below) against the real R2. Before the first run, the
project name was confirmed different from the live stack's (`optlab-restore-test` against
`optlab`; all live containers and `optlab_pgdata` carry the `optlab` label; a `--dry-run` of the
cleanup listed only the restore project's own resources). The script pins
`-p optlab-restore-test` and logs that dry run before every cleanup; `db`, `recorder`, `health`,
`jobs`, `dashboard` and `optlab_pgdata` were untouched both times.

**2. The gap report covers every recorded day: PASS.** `optlab-data gap-report` (15 Nov
01:09 UTC):

| UTC day | hours checked | minutes recorded / expected | option rows | skipped minutes | longest gap | findings |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-13 | 8/8 | 447/447 | 715,682 | 1 | 2 min | partial_snapshot 1 |
| 2026-11-14 | 24/24 | 1440/1440 | 2,325,985 | 1 | 2 min | empty_book 779, partial_snapshot 1, stale_index 2 |
| 2026-11-15 | 1/1 | 60/60 | 100,200 | 0 | 1 min | empty_book 3 |

Skipped periods (backtests leave these minutes out):

- 2026-11-13 21:11 to 21:12 UTC (1 min): partial_snapshot
- 2026-11-14 20:20 to 20:21 UTC (1 min): partial_snapshot, stale_index

Two minutes in 1,947 are skipped, both partial snapshots when one Deribit connection dropped
just before a minute boundary (the second one carried both indexes, so the index check flags
it too). No minute is missing; "longest gap" is the time between two usable minutes (1 when
nothing is skipped), so 2 minutes means one skipped minute. Twice in about 32 hours is not often, so the
short-reconnect change in `docs/later.md` stays parked, as Damian asked. No crossed books,
stale quotes or IV outliers. The empty-book findings are far-from-the-money options with
neither a bid nor an ask; they don't remove a minute.

**3. The liquidity report names the book: BTC_USDC (linear).** `research/liquidity_report.md`,
over 13 Nov 16:33 to 14 Nov 23:59 UTC synced back from R2 (combined dataset version
`017271df668bbca8`), sampling every 5th of 1,885 usable minutes (377):

| Measure | BTC_USDC (linear) | BTC (inverse) |
| --- | --- | --- |
| Minutes with a spread passing the v1 liquidity rule | 376 of 377 | 372 of 377 |
| Candidate spreads passing the rule | 88% of 1,784 | 40% of 3,724 |
| ... both legs bid-ask at most 10% of mid | 89% | 44% |
| ... top-of-book size covers the order (median 1.16 BTC) | 98% | 94% |
| Median bid-ask of short / long leg (of mid) | 4% / 6% | 7% / 10% |
| Median short-leg bid size / long-leg ask size (BTC) | 10.00 / 10.00 | 76.20 / 63.05 |
| Expiries in the 7-30 day window | 27 Nov | 27 Nov, 4 Dec |
| Option trades in the book (on candidate legs) | 1,406 (181) | 18,710 (3,133) |
| Minutes with a liquid spread whose credit is at least 20% of width | 46% | 27% |

Recommendation: keep **BTC_USDC** as the strategies' book, as the brief planned; it is not
too thin. Its quotes are tighter, so far more candidate spreads pass the v1 liquidity rule, and
its top-of-book size covers the order the sizing rule allows. The inverse book trades much
more, but v1 crosses the spread at quoted prices and sizes, which is what the rule measures.
Caveats (in the report): few USDC trades print; USDC lists fewer expiries (no 4 Dec), so from
20 Nov 08:00 to 25 Nov 08:00 UTC no USDC expiry is in the 7-30 day window unless Deribit lists
one; the minimum credit binds more than liquidity; about 31 hours of data, most of it a
Saturday. The inverse book keeps being recorded as the fallback, and the report re-runs on more
data. Damian to confirm.

**Nightly job, unattended: failed once, fixed, then backed up.**

- *The failure.* The first unattended run (15 Nov 00:30 UTC, for 14 Nov) exported the day
  (2,325,985 option rows), then stopped before uploading anything. On R2, `rclone cat` of a key
  that doesn't exist yet exits 0 and prints nothing (checked on the live bucket). So the new
  "never replace R2" check read an empty manifest and failed with a JSON error.
- *What happened then.* The failure ping went out at 00:32, Healthchecks turned "nightly
  backup" red, and nothing in R2 changed. The tests had missed it because rclone's local
  backend answers "not found" instead.
- *The fix* (`27c0678`). `Rclone.get` lists the folder with `lsf` first and runs `cat` only for
  a listed object; a listed object that reads back empty is an error. The tests' stand-in for
  R2 now answers reads of missing keys exactly as R2 does, and before the fix the backup tests
  failed on it with the server's error.
- *Recovery.* The fix was deployed with the scheduler paused, and 14 Nov backed up by hand at
  00:55–00:57: 2,380,077 rows, 78.3 MB export, 50 raw files 322.7 MB (the first full day of
  raw stream, a third of the 1 GB warning level), dataset version `56a96323d91c…`. Every file
  was checked in R2 and the manifest read back. The scheduler then restarted on the new image
  (catch-up: nothing new, ping OK).
- *Now.* Damian confirmed all three Healthchecks checks green again, "nightly backup" included.
  R2 holds both days under `exports/v1/manifests/` and `raw/manifests/`. The next unattended
  run is 16 Nov 00:30 UTC.

## Phase 2 · Data and checks: what was built (14–15 Nov 2026)

Commits `c845d1a`, `834d1f5`, `26ec2f9`, `3d7b283`, `27c0678` and the closing commit; CI green
on each (runs 43641426254, 43642371551, 43644533910, 43645068899, 43660457256 and the closing
commit's).

1. **Nightly export and R2 backup** (`optlab.market_data.export`, `backup`, `r2`; `jobs` service
   at 00:30 UTC). Each finished UTC day of `option_quotes`, `underlying_ticks` and
   `option_trades` (plus `instruments`, `delivery_prices`, `connection_events` and the new
   status and data-quality tables, which backtests and the restore need) goes to zstd Parquet,
   partitioned by date and, for options, by expiry, sorted by instrument then time.
   - *Manifest.* It lists every file (rows, bytes, SHA-256) and every table (rows, PostgreSQL
     content checksum). Its SHA-256 is the dataset version (`combined_version` for several
     days).
   - *R2 layout.* `raw/` holds the raw stream (kept 18 months) and `exports/v1/` the exports
     (kept for good). Files are checked in R2 (size and MD5) before the manifests go up, last.
   - *Server copies.* The server keeps 7 days of raw and export files; older ones are removed
     only once found again in R2.
   - *Pings.* Success pings `HC_PING_URL_BACKUP`; a failure sends a failure ping and retries
     hourly.
2. **R2 only grows.**
   - `Rclone.run` accepts only `copy`, `check`, `cat` and `lsf`.
   - Every upload is `copy --immutable --checksum`, so an object already in R2 is never
     modified. `copyto` is refused because rclone ignores `--immutable` for it (checked).
   - A backup stops before uploading if R2 already holds a different manifest for the day (for
     example a re-run after the server's raw files were removed). Whether a manifest exists is
     decided by listing it, never by what `cat` prints.
   - The rclone remote is built from the four `R2_` variables in each rclone process's
     environment, with `no_check_bucket` and no config file. rclone's messages are scrubbed of
     keys and the endpoint.
   - rclone 1.75.2 comes from the official `rclone/rclone` image, pinned by digest.
3. **Restore test** (`optlab.market_data.restore`, `infra/restore-test`,
   `infra/restore-test.compose.yaml`): refuses any database that already has tables; monthly
   via `optlab-restore-test.timer` (1st of the month, 03:00 UTC, as `deploy`); a failure pings
   `HC_PING_URL_BACKUP` as failed. No container has the Docker socket.
4. **Hourly data checks** (`optlab.market_data.quality`, thresholds in
   `configs/data_quality.yaml`):
   - missing and partial snapshots and a stale index, which mark minutes to skip;
   - crossed and empty books, stale quotes and IV outliers;
   - one row per finding in `data_quality`, one per checked hour in `data_quality_runs`, and
     `optlab-data gap-report` to show them.
5. **Point-in-time `MarketDataView`** (`optlab.market_data.view`) over Parquet days
   (`ParquetSource`) and the database (`DatabaseSource`, read-only), with `snapshot_times`
   leaving out the skip periods.
   - Each table has a "known from" time (snapshot minute, trade receive time, first seen,
     recorded). Later-changing instrument columns are hidden, and a data-quality period still
     open at t is cut off at t.
   - The leakage test changes, deletes and adds rows after t in a second copy of the market,
     and checks every method returns identical tables at t, over both sources. It caught one
     real leak (a period ending just after t), now fixed.
   - A second test checks the two sources return identical tables.
6. **Liquidity report** (`optlab.evaluation.liquidity`, `research/liquidity_report.py`).
7. **Status page** (`apps/dashboard/status.py`, `dashboard` service on the server's
   127.0.0.1:8501, read-only connection): last tick per stream, recorder uptime, restarts,
   memory and connections, recent gaps, data volume per day with R2 status, warnings for
   stale streams and for any day over 1 GB of raw files.
8. **Laptop sync** (`optlab-data sync DAY... | --from/--to | --list | --raw`): downloads only
   missing or mismatched files and checks every SHA-256; the manifest is written last.
   Exercised against the real R2 from a one-off container on the server (both days, for the
   liquidity report).
9. **Tardis importer: not done** (item 8, "only if time allows"); in `docs/later.md`.

Also, from Damian's 14 Nov decisions and `docs/later.md`:

- **Raw-stream cap:** 1 GB a day is a warning level only. The recorder logs a warning once per
  day past it and the status page shows it; nothing is thinned or dropped. The first full day
  (14 Nov) wrote 322.7 MB.
- **Recorder status every minute** in the log and in `recorder_status`: memory (resident and
  peak), connections, message rate, raw bytes written so far today.
- **Who closed a connection:** disconnects now say "closed by server", "closed by us" (with the
  code sent, such as the library's keepalive 1011) or "none sent or received: the TCP
  connection ended". Since the 14 Nov 21:29 restart there were 6, all on 14 Nov between 22:26
  and 23:39 UTC, on four of the five connections. All six were the last kind: the TCP
  connection ended with no close frame from either side, so neither Deribit nor the recorder
  closed it. Each reconnected (6 reconnections in that window, all 5 connections up since),
  and none cost a snapshot minute or made one partial.

Migration `0002` adds `data_quality`, `data_quality_runs`, `recorder_status`,
`dataset_exports` and `utc_ms()`; no recorded table changes. Tests: 199, including the database,
rclone (real binary, local folder standing in for R2, answering missing keys as R2 does) and
Streamlit page tests; CI installs rclone from the same pinned image and fails rather than skips
without it.

**Deploy on recorder-1 (14–15 Nov 2026).**

- *Image.* Built on the server (1.64 GB, with rclone and Streamlit).
- *Backup before the migration.* `backups/pre-migration-20261114T2121Z.dump`, 126 MB, read
  back with `pg_restore --list` (31 tables with data, including all six recorder tables and 16
  hypertable chunks).
- *Migration and restart.* Migration `0002` applied. `recorder`, `health` and `dashboard`
  restarted at 21:29:02, two seconds after a minute boundary: no snapshot minute was missed
  (1,648 to 1,650 options per minute before and after).
- *First runs.* First backup by hand at 21:32 (13 Nov: 731,637 rows, 25.3 MB export, 16 raw
  files 83.6 MB; ping OK); restore test at 21:35; `jobs` started at 21:46 (catch-up found
  nothing new and pinged OK).
- *Timer.* Installed (next run Tue 1 Dec 03:09 UTC). `backups/` is now in `.gitignore` (it
  showed as untracked on the server).
- *The fix.* `27c0678` deployed on 15 Nov at 00:52–01:00: `jobs` stopped, image rebuilt, 14 Nov
  backed up by hand, `jobs` started again with `up --no-deps`. The other services kept running.

## Phase 1 · Recorder: gate evidence (14 Nov 2026)

Checked at 2026-11-14 18:55 UTC on recorder-1, read-only, at commit `89c4f6f` (the deployed
code; `1e8824d` only changed this log).

**Gaps: PASS.** `infra/dc exec -T db psql -U optlab -d optlab -x < infra/sql/gap_check.sql`:

| Field | Result |
| --- | --- |
| window | 2026-11-13 18:54 → 2026-11-14 18:54 UTC (24 h, ending at the last full minute) |
| minutes present / expected | 1441 / 1441 |
| option rows per minute | 1206 to 1630 |
| longest gap between snapshots | 00:01:00 |
| gaps over 5 minutes | 0 |
| breaks | none |
| gate | **PASS** |

The one short minute was 2026-11-13 21:11 (1,206 rows instead of 1,602): connection
`deribit-ws-3` dropped at 21:10:59, one second before the snapshot, and its ~400 instruments
were left out of that minute rather than written with prices from a dead connection. The same
minute is the only one missing for four BTC_USDC futures carried on that connection.

**Stability.** All three services up 26 hours, 0 restarts. 24 disconnects over 26 hours, all
"connection closed without a close frame", each back within about a second (connected and
resubscribed); only the 21:11 one cost data.

**BTC_USDC linear dated futures (Damian's question).** All recorded. On 14 Nov Deribit lists 8
BTC_USDC option expiries (15, 16, 17, 18, 20, 27 Nov, 25 Dec, 26 Mar) and one BTC_USDC dated
future for each (checked against `public/get_instruments` live and against `instruments`).
Each has 1,439 or 1,440 minutes in `underlying_ticks` over the last 24 hours (currency USDC).
BTC_USDC-18NOV26, listed at 08:00, was picked up by the 08:00:30 discovery (first row 08:01);
BTC_USDC-14NOV26 was recorded up to its 08:00 expiry and then marked inactive. Nothing was
changed on the server.

**Day-one volume** (`infra/sql/volume.sql`, `du`, and a one-off export measurement):

| Item | Day one |
| --- | --- |
| `option_quotes` | 2,312,204 rows per 24 h; 860 MB in the database after 26 h (616 MB table, 244 MB indexes), uncompressed until chunks are 7 days old |
| `underlying_ticks` | 34,580 rows per 24 h; 9 MB |
| `option_trades` | 14,259 trades per 24 h; 4.6 MB |
| whole database | 884 MB after 26 h |
| raw quotes (24 complete hours, 13 Nov 18:00 → 14 Nov 18:00) | 267.9 MB (busiest hour 22.4 MB, quietest 5.1 MB) |
| raw trades (same hours) | 1.0 MB |
| one day as zstd Parquet (what Phase 2 uploads) | `option_quotes` 81.3 MB (35 B/row), `underlying_ticks` 0.8 MB, `option_trades` 0.5 MB: 82.6 MB |
| R2 growth at the day-one rate | 0.35 GB a day (0.27 raw + 0.08 exports), about 10.7 GB a month |

Server resources: disk 14% (11 of 77 GB; the jump from 5.3 GB is mostly one-off Docker images
3.1 GB and build cache 0.9 GB); memory 2.3 GiB available. The recorder's own memory is 340 MB
(flat over 10 minutes; current 387 MB RSS below its 425 MB peak, so a busy-hour high-water mark
rather than a leak so far); the database uses 742 MB of its 1.5 GB limit.

Day one was a Friday evening into a Saturday, usually quieter than a weekday, so weekday and
volatile days will be larger.

**Raw-stream cap (proposed here; Damian decided on 14 Nov: 1 GB a day as a warning level
only, raw files 18 months in R2, exports for good; see Phase 2).** R2 charges $0.015 per GB-month above the
free 10 GB (before any VAT), so $10 a month is about 677 GB stored. Storage only grows, so the
proposal has two parts:

1. **At most 1 GB of raw files per UTC day** (about 3.7 times day one), with a warning at
   0.75 GB. Past 1 GB, the best bid/ask stream is thinned for the rest of that UTC day to at
   most one change per instrument per second; trades are never thinned.
2. **Raw files kept 18 months in R2**; the database exports are kept for good (2.5 GB a month).

| Months of recording | Stored at day-one rate | R2 cost | Stored if every day hits the cap | R2 cost |
| --- | --- | --- | --- | --- |
| 12 | 128 GB | $1.77 | 395 GB | $5.77 |
| 24 | 207 GB | $2.96 | 607 GB | $8.96 |
| 48 | 268 GB | $3.87 | 668 GB | $9.87 |

So R2 stays under $10 a month for about four years even if every single day hits the cap, and
near $2 to $4 a month at day-one volume. Without retention, day-one volume reaches $10 a month
after about five years, and a constant 1 GB a day after about 21 months. Class A writes (about
50 files a day plus the nightly export) stay far inside the free million a month. The cap and
retention are built in Phase 2 with the R2 upload, once Damian chooses.

## Phase 1 · Recorder: build and deploy (13 Nov 2026)

What was built (commit `89c4f6f`, CI run 43457696031 green: lint, types, 138 tests including
the database tests, Docker image build, full-history secret scan):

- **Schema** (Alembic `0001`): `instruments`, `option_quotes`, `underlying_ticks`,
  `option_trades` (one row per `trade_id`), `delivery_prices` (exact `numeric`),
  `connection_events`. `option_quotes` and `underlying_ticks` are hypertables with one-day
  chunks, compressed after 7 days (segment by instrument).
- **Deribit adapter** (`src/optlab/market_data/deribit/`): public methods only (anything else
  raises); JSON-RPC ids mapped to futures; `public/set_heartbeat` (10 s) with `test_request`
  answered by `public/test`; a connection silent for 30 s is treated as dead; reconnect with
  exponential backoff (1 s to 60 s, ±20% jitter) and resubscribe; token buckets below Deribit's
  documented limits; at most 400 channels per connection. Deribit JSON is decoded into
  msgspec structs that never leave the adapter.
- **Discovery** every 10 minutes and at 08:00:30 UTC, from instrument metadata only.
- **Recorder**: one row per instrument per minute, point in time (the snapshot is taken before
  the first message received after the minute is applied), COPY into a staging table then
  `INSERT ... ON CONFLICT DO NOTHING`; trades every 5 s; delivery prices at start and at 08:01,
  08:05, 08:15 and 08:30 UTC; every connect, disconnect and reconnect in `connection_events`.
- **Raw stream**: best bid/ask changes (from the 100 ms ticker) and option trades in hourly
  zstd Parquet files under `data/raw/{quotes,trades}/YYYY-MM-DD/`.
- **Health service**: recorder ping only when `option_quotes` has rows from the last 2 minutes;
  disk ping every 10 minutes, failure pings marked WARNING at 70% and CRITICAL at 85%.
- **Deploy**: `infra/compose.yaml` (`db`, `recorder`, `health`; no published ports;
  `restart: unless-stopped`), run through `infra/dc`. Steps in `infra/server-setup.md` §14.

Conventions applied at the adapter boundary: Deribit sends IVs in percent; they are stored as
decimals (41.25 → 0.4125). An empty side of the book (Deribit: price 0) is stored as a NULL
price with size 0. Each price row carries its currency (BTC for inverse option premiums, USDC
for linear, USD for inverse futures and `btc_usd`). Greeks are stored in Deribit's units.

Checked against the live API (12–13 Nov) before and after building:

- Deribit's API matches the brief: both option books, both perpetuals with funding, dated
  futures in both books, the `btc_usd` and `btc_usdc` indexes and delivery prices are public.
- The universe on 13 Nov: 1,600 BTC options (both books), 2 perpetuals, 20 dated futures,
  2 indexes: 1,626 channels on 5 connections, about 1,700 messages a second.
- Deribit accepts subscriptions to unknown instruments silently (they are just missing from
  the reply); the adapter compares the reply and records refusals as `subscribe_rejected`.

Deploy on recorder-1 (13 Nov 2026, 16:24–16:35 UTC):

- `DATABASE_URL` was generated on the server (`openssl rand -hex 32`) and written into `.env`
  without being printed; a format check printed `1`; `.env` stayed `-rw------- deploy`.
- Image built on the server; `db` healthy (TimescaleDB 2.30.2, PostgreSQL 17); a pre-migration
  dump was taken (`backups/pre-migration-20261113T1629Z.dump`, empty database); migration
  `0001` applied; both hypertables report compression on with a 7-day policy.
- After 2.5 minutes: 3 services up; 1,600 `option_quotes` rows in each of 16:33 and 16:34 and
  24 `underlying_ticks` rows; 5/5 connections, about 1,740 msg/s, 0 decode errors; both
  healthchecks.io pings answered OK. Recorder 22% of one CPU and 84 MiB, database 91 MiB;
  3.1 GiB of memory still available; disk 7% used.

Tests: the adapter is tested against a local fake Deribit server that replays messages in the
format recorded from the live feed (values made up, because raw vendor data is never
committed), including dropped connections, close code 4000, silence, refused channels and
garbage messages. A full recorder run against the fake server cuts every connection and checks
that it reconnects, resubscribes and keeps writing. Database tests run against TimescaleDB,
including the gate's gap query (a 4-minute hole passes, a 5-minute hole fails).

## Phase 0 · Setup: gate evidence

Gate checked on 2026-11-10 00:21 UTC, with fresh SSH connections from the laptop:

| Check | Result |
| --- | --- |
| CI is green | GitHub Actions run 42725115101 on commit `0a42884`: **success** (both jobs: "Lint, types and tests" and "Secret scan (gitleaks, full history)") |
| Damian can SSH in as `deploy` | `ssh deploy@SERVER_IP` → `logged in as deploy on recorder-1` (key `damian-laptop`, SHA256:4ztd2bk0…) |
| Root login is refused | `ssh root@SERVER_IP` → `Permission denied (publickey).` |
| `docker run hello-world` works | As `deploy`, without sudo → `Hello from Docker!` |

Also verified, after the reboot (22:30 UTC on 9 Nov):

- **SSH settings in effect:** `permitrootlogin no`, `passwordauthentication no`,
  `kbdinteractiveauthentication no`, `authenticationmethods publickey`. A password-only login as
  deploy is refused with `Permission denied (publickey)`.
- **Automatic security updates:** unattended-upgrades active, `Update-Package-Lists "1"`,
  `Unattended-Upgrade "1"`.
- **Time:** time zone Etc/UTC, clock synchronized, chrony tracking a Canonical NTS server
  (12 µs off).
- **Docker:** 29.9.3 with Compose v5.7.0, active and enabled at boot; `json-file` logs capped at
  5 × 10 MB, compressed.
- **Swap:** 2 GB `/swapfile` in fstab, swappiness 10.
- **Updates:** all 42 installed; 0 waiting; no reboot flag; kernel 7.0.0-43.
- **Web Console agent:** `droplet-agent` active. ufw left off, as decided.
- **Repo on the server:** `/srv/btc-options-lab` cloned with the read-only deploy key "recorder-1
  (read-only)" (SHA256:qZv0igcj…). `.env` was copied from `.env.example`, mode 600, ignored by git,
  values blank.
- **Locally:** 36 tests pass; ruff, mypy (strict) and every pre-commit hook pass; a gitleaks scan
  of the full history found no leaks; the server IP appears in no repo file; Claude Code's read of
  `.env.example` was refused by the deny rule.
- **Backups and alerts:** the R2 bucket `btc-options-backup` with its key (28 Oct) and the three
  healthchecks.io checks (27 Oct) were set up by Damian, who pasted the four `R2_` values and
  the three ping URLs into the server's `.env` himself on 10 Nov.

Decisions made during Phase 0 (9 Nov):

- `deploy` has passwordless sudo and is in the `docker` group.
- ufw stays off; the DigitalOcean cloud firewall `ssh-only` is the firewall.
- Updates were installed and the server rebooted at the end of hardening.
- The deploy key was created by Claude Code and added through `gh`.
- Docker is updated by hand (not automatic); no automatic reboots.

## Next

1. Damian, by hand (details in the Phase 2 report):
   - add the R2 lifecycle rule in the Cloudflare dashboard: prefix `raw/`, delete objects 548
     days (18 months) after upload. The R2 token is Object Read & Write only, which cannot set
     bucket rules;
   - for laptop backtests: `sudo apt install rclone`, and put the four `R2_` lines in the
     laptop's own `.env` (the same token: it has no IP filter);
   - confirm the book (BTC_USDC) and the data-quality thresholds, and answer the USDC expiry
     question below.
2. Phase 2B · Rough end-to-end version (Prompt 2B).

## Open questions

- **Book:** the liquidity report (`research/liquidity_report.md`) recommends BTC_USDC (linear).
  Confirm, or choose inverse.
- **When BTC_USDC has no expiry 7 to 30 days away.** On 14 Nov, BTC_USDC listed no 4 Dec expiry
  (inverse did). Unless Deribit lists one, no USDC expiry is in the v1 window from 20 Nov 08:00
  to 25 Nov 08:00 UTC. Should v1 then make no entry (simplest), or fall back to inverse?
  The recorder will show whether Deribit adds USDC expiries; needed by Phase 2B's paper service.
- **Data-quality thresholds** (`configs/data_quality.yaml`, proposed from the first 30 hours):
  partial snapshot under 98% of listed options; stale quote over 120 s; mark IV outside 0.05
  to 5.0 or moving over 0.10 in a minute (2+ days to expiry); index missing, over 60 s old or
  unchanged for 5 minutes. Missing and partial snapshots and a stale index mark minutes to
  skip.
- **"No entry while any data-quality flag is active"** (v1 rules): flags are global (missing
  or partial snapshot, stale index) or per instrument (crossed, empty or stale book, IV
  outlier). Should an instrument flag block only spreads using that instrument (proposed), or
  all entries? Needed by Phase 5.
- **Minimum credit:** in the liquidity report, "credit at least 20% of width" (before fees)
  holds for 10% of USDC candidate spreads (5% inverse); a liquid spread meeting it existed in
  46% of sampled minutes (27% inverse). So the minimum credit, not liquidity, will limit
  entries. Not a data problem; worth knowing before Phase 5.
- **Open interest delay.** Deribit's ticker docs say `open_interest` is reported with a delay.
  Stored as given; Phase 3 should check it before using it.
- JEV details (inputs, outputs, determinism, speed and cost per call, training dates, whether it
  can be self-hosted). Needed by week 4.
- The DigitalOcean metrics agent (`do-agent`) is not installed on recorder-1, although the brief
  says monitoring is on. Install it from the Droplet's Graphs tab if you want the graphs (see
  `docs/later.md`).

Closed on 14 Nov 2026:

- **Two-step settlement:** answered by the brief's new Settlement note (refreshed 14 Nov). An
  in-the-money option becomes a position in its expiry's future at the strike, which cash-settles
  at the delivery price: the same payoff. Backtests pay intrinsic value at the delivery price
  and charge the delivery fee once (rate from Deribit's fee page in Phase 2B). Linear options
  since early April 2026, inverse since 1 August 2026.
- **Old-site API keys:** not reused; any key this project needs is made fresh (decided 14 Nov).
- **Raw-stream cap:** 1 GB a day as a warning level, raw files 18 months in R2, exports for good.
- **Disconnects:** logging now says which side closed (see Phase 2).
- **R2 keys and backup ping URL:** in the server's `.env` since 10 Nov (pasted by Damian).
