# Progress log

Read at the start of every session, updated at the end: what's done, what's next, open
questions. A phase ends only when its gate passes, with the evidence recorded here.

## Phases

| Phase | When | Gate before the next phase | Status |
| --- | --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | CI is green, Damian can SSH in as `deploy`, root login is refused, and `docker run hello-world` works on the server (backups and alerts configured) | **Passed** 10 Nov 2026 |
| 1 · Recorder live | Week 1 · days 2–4 | 24 h with no gap over 5 min in `option_quotes`; day-one volume measured and the raw-stream cap set from it | **Passed** 14 Nov 2026 (raw-stream cap proposed, Damian to choose) |
| 2 · Data and checks | Week 1 · days 4–7 | Restore from R2 works; the gap report covers every recorded day; the liquidity report names the book | Open |
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

Phase 1 · Recorder live: **passed** on 14 November 2026. The recorder keeps running on
recorder-1 (since 2026-11-13 16:32 UTC, no restarts). Phase 2 has not started.

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

**Proposed raw-stream cap (for Damian to choose).** R2 charges $0.015 per GB-month above the
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
  healthchecks.io checks (27 Oct) were set up by Damian. Their values go into the server's `.env`
  before Phases 1 and 2.

Decisions made during Phase 0 (9 Nov):

- `deploy` has passwordless sudo and is in the `docker` group.
- ufw stays off; the DigitalOcean cloud firewall `ssh-only` is the firewall.
- Updates were installed and the server rebooted at the end of hardening.
- The deploy key was created by Claude Code and added through `gh`.
- Docker is updated by hand (not automatic); no automatic reboots.

## Next

1. Damian chooses the raw-stream cap and R2 retention (proposal above).
2. Phase 2 · Data and checks (Prompt 2), including the chosen cap. Before it starts, Damian
   pastes the R2 keys and `HC_PING_URL_BACKUP` into the server's `.env`. Until Phase 2 moves
   raw files to R2, they stay on the server (about 0.3 GB a day, ample room).

## Open questions

- **Raw-stream cap**: 1 GB a day with thinning past it, and 18 months of raw files in R2?
  (Phase 1 gate evidence above.)
- **Disconnects**: 24 in 26 hours, all closed without a close frame, recovered in about a
  second. The log can't yet say whether Deribit, the network or our own keepalive closed them;
  `docs/later.md` has the small logging change that would tell.

- **Two-step USDC settlement.** The brief says linear USDC options settle in two steps since
  April 2026. Deribit's API docs and changelogs (read 12 Nov) don't mention it. The recorder
  stores the `btc_usdc` and `btc_usd` delivery prices either way; Phase 4 needs the exact rule
  from Damian or Deribit's support pages.
- **Open interest delay.** Deribit's ticker docs say `open_interest` is reported with a delay.
  Stored as given; Phase 3 should check it before using it.

- JEV details (inputs, outputs, determinism, speed and cost per call, training dates, whether it
  can be self-hosted). Needed by week 4.
- The DigitalOcean metrics agent (`do-agent`) is not installed on recorder-1, although the brief
  says monitoring is on. Install it from the Droplet's Graphs tab if you want the graphs (see
  `docs/later.md`).
- The brief says API keys created for the old site are revoked before Phase 0 ends. Damian to
  confirm this is done (or that none exist).
