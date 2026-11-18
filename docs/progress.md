# Progress log

Read at the start of every session, updated at the end: what's done, what's next, open
questions. A phase ends only when its gate passes, with the evidence recorded here.

## Phases

| Phase | When | Gate before the next phase | Status |
| --- | --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | CI is green, Damian can SSH in as `deploy`, root login is refused, and `docker run hello-world` works on the server (backups and alerts configured) | **Passed** 10 Nov 2026 |
| 1 · Recorder live | Week 1 · days 2–4 | 24 h with no gap over 5 min in `option_quotes`; day-one volume measured and the raw-stream cap set from it | **Passed** 14 Nov 2026 (raw-stream cap chosen 14 Nov) |
| 2 · Data and checks | Week 1 · days 4–7 | Restore from R2 works; the gap report covers every recorded day; the liquidity report names the book | **Passed** 15 Nov 2026 |
| 2B · Rough end-to-end version | End of week 1 | Paper service runs 24 h and shows its trades; shortcut list exists (results don't count yet) | **Gate open** (checked 17 Nov 2026): ran over 24 h and decides daily, shortcut list exists, but no rough trade yet; open until the first one shows (Damian's option A), Phase 3 may start meanwhile |
| 3 · Pricing and surface | Week 2 | IVs and Greeks match Deribit and finite differences within agreed tolerances; golden tests match btc-vol-models; live surface on the status page | **Passed** 18 Nov 2026 (deployed 01:53 UTC; tolerances agreed and checked on 17 Nov data, all 22 checks pass; formulas and accounting signed off) |
| 4 · Backtest engine | Weeks 2–3 | Determinism, leakage and reconciliation tests pass on a real week; hand-checked smoke trade matches | Open |
| 5 · Strategies and results | Week 3 | Full report of v1 rules vs every benchmark on the locked test, after costs | Open |
| 6 · Streamlit dashboard | Weeks 3–4, in parallel | Public site loads every page on laptop and phone; nothing on it changes the system | Open |
| 7 · JEV challenger | Week 4 | JEV-versus-rules report on the same frozen folds, seeds and cost model | Open |
| 8 · Testnet paper trading | Weeks 3–4, runs a week | A week of paper decisions matches its replay, every difference explained | Open |
| 9 · Release v1.0 | End of week 4 | A fresh clone runs end to end with one command; history scan is clean | Open |
| 10 · Challenger models | Week 5, after v1.0 | Each challenger promoted or rejected on evidence | Open |

## Current phase

Phase 3 · Pricing and volatility surface: **gate passed on 18 Nov 2026** (below).

Phase 4 · Backtest engine: **next, not started**. Its gate needs the first full recorded week
(14 to 20 Nov), complete from **Sat 21 Nov 2026** (synced after that night's 00:30 UTC
export); the build itself may start earlier.

Settled ahead of Phase 4 (18 Nov 2026):

- **Read-only R2 token: created by Damian** (about 03:50 UTC) in the Cloudflare dashboard. The
  laptop has no `.env` yet (checked without reading: no file; `.env` is git-ignored and not
  tracked). In Phase 4 Damian creates it and pastes `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_ENDPOINT` and `R2_BUCKET` (the field names in `src/optlab/config.py`) himself. rclone is
  not installed in WSL yet; Phase 4 installs the pinned 1.75.2.
- **Margin model (add-on f): Phase 4.** Damian left the choice to Claude Code ("the most
  optimal choice"). Reason: Phase 8 runs the same risk gateway as the backtests, so a margin
  check built once in Phase 4's gateway makes Phase 5's results respect margin from the start
  (no re-run later), and Phase 8 then compares our margin estimate with testnet's actual
  margin. Scope: a simple, conservative initial and maintenance margin check from Deribit's
  published rules for the account type we assume, with the source and any simplification
  written down; an order that would breach it is rejected and logged in `risk_events`. With
  100,000 USDC and the brief's limits (total maximum loss at most 10% of equity) it is
  expected to bind rarely; it is a guard, not a new trading rule.

Phase 2B · Rough end-to-end version: **running** since 15 November 2026 15:30 UTC. Gate
checked on 17 Nov (below): two of its three parts pass; "shows its trades" waits for the first
rough trade. Under Damian's option A the gate stays open until then, and Phase 3 may start
meanwhile. The rules are not loosened to force a trade.

Running on recorder-1, at commit `3bed9c3` (database at `0004`, since 18 Nov 01:53 UTC):

- `db`, `recorder` and `health`;
- `jobs`: data checks hourly, R2 backup nightly at 00:30 UTC;
- `dashboard`: the status page, with the "Paper trading (ROUGH)" and "Volatility surface"
  tabs, on the server's localhost only;
- `paper`: the ROUGH paper service;
- `surface`: the Phase 3 surface worker (both books, every minute).

The restore test runs monthly from a systemd timer.

Since 17 Nov 01:09 UTC the server also runs another of Damian's projects (Compose project
`btc-dashboard`, not part of this repo); see "Open questions".

## Phase 3 · Pricing and volatility surface (18 Nov 2026): gate passed

**Gate passed on 18 Nov 2026 at about 03:00 UTC**, in a fresh conversation started at about
02:20 UTC from the Windows handover folder.

**Damian's answers (18 Nov 2026, about 02:30 UTC: "Yes, all 8", each recommendation accepted):**

1. **Tolerances** (add-on g): agreed as proposed (the list under "Evidence so far" below). They
   are now code: `TOLERANCES` and `gate_checks` in `optlab.evaluation.deribit_check`, with
   tests that pin the agreed numbers, so they can't be widened quietly.
2. **The rough paper service** (add-on c): **not switched** to the new IV, forward and
   realised-vol code. It keeps its rule until Phase 4/5 replaces it, as its own gate is still
   open (`docs/rough-version.md`, "Phase 3 status").
3. Daily cut 00:00 UTC; 4. risk-free rate 0; 5. VaR and ES by historical simulation on daily
   returns; 6. realised vol on 08:00 UTC closes (`docs/formulas.md`, choices 1 to 4).
7. Open positions at Deribit's mark, liquidation value alongside; 8. BTC amounts in USDC at the
   same minute's `btc_usdc` index (`docs/accounting.md`, choices 5 and 6).

So `docs/formulas.md` and `docs/accounting.md` are signed off for Phase 4.

**Gate evidence (18 Nov 2026, server clock):**

- **A fresh recorded day, read-only.** 17 Nov 2026 (a different day from the 16 Nov sample the
  tolerances came from) was pulled at 02:48:20-02:48:32 UTC by `SELECT ... COPY TO STDOUT`
  over SSH into the session's scratch folder: every 10th minute of option quotes (223,114
  rows), the day's futures and indexes (31,655), instruments (2,184), trades (7,942) and every
  minute's open interest 12:00-18:00. Nothing on the server changed; no raw data in the repo.
- **`research/deribit_comparison.py` on 17 Nov** (144 minutes, both books, 223,114 quotes for
  the engine check, 161,782 for the surface check; 288 surfaces in 100 s): **all 22 checks
  pass** (`research/deribit_comparison.md`, section "Gate"):

  | Check (window: 7-30 days, abs delta 0.05-0.50) | Tolerance | BTC_USDC | BTC |
  | --- | --- | --- | --- |
  | Engine delta, max (1 day or more) | 0.0001 | 0.000042 | 0.000047 |
  | Engine gamma per USD, max (1 day or more) | 0.00001 | 0.0000050 | 0.0000050 |
  | Engine vega, USD per vol point, max (1 day or more) | 0.05 | 0.016 | 0.028 |
  | Engine theta, USD a day, max (7 days or more) | 0.05 | 0.014 | 0.014 |
  | Engine IV from the mark price, 99th pct (vol points) | 0.5 | 0.24 | 0.25 |
  | Surface IV against the mark, median (vol points) | 0.25 | 0.11 | 0.09 |
  | Surface IV against the mark, 99th pct (vol points) | 1.0 | 0.55 | 0.44 |
  | Surface delta against Deribit's, 99th pct | 0.005 | 0.0030 | 0.0022 |
  | 0.20-delta short leg: share picking another strike | 2% | 0.7% (2 of 288) | 1.0% (6 of 576) |
  | ... and how far apart | 1 strike | 1 | 1 |
  | Forward, future against parity, 95th pct | 10 bp | 4.3 bp | 3.9 bp |

  One BTC_USDC expiry (27 Nov) was 7 to 30 days out on 17 Nov, so the BTC_USDC window checks
  rest on that expiry; the inverse book had two.
- **Golden tests** against btc-vol-models (`4bb1cb1`) pass, with the rest of the suite: 1,505
  pass on the laptop with the test database (9 rclone tests skip there; CI runs them all);
  ruff, ruff format and mypy (strict) clean.
- **The live surface** (read-only, 02:52 UTC): `vol_surface` holds every minute since the
  deploy (60 of 60 minutes 01:53-02:52, 4,200 rows, 35 points per book per minute), the newest
  35 s old; `features` likewise; 0 errors in the surface worker's last hour; seven services Up,
  0 restarts. The status page through an SSH tunnel at 02:53: the "Volatility surface" tab shows
  the surface minute 6 s old, BTC_USDC ATM IV 30 d 41.46% against RV30 44.29%, the surface grid,
  the latest smile (forward from the future, -0.2 bp from parity, fit RMSE 0.15 vol points,
  butterfly and calendar ok), the expiries and ATM IV against RV.

### Build and deploy record

Built at commit `4c0a907` (01:33 UTC; CI run 44216767478 green: secret scan, lint, types,
every test with the database and rclone, Docker image). Paused at Damian's request at about
01:40 UTC (server clock 01:40:58 at the last read-only check); what is left is under "Current
phase" above.

Started 18 Nov 2026 at about 00:35 UTC from the Windows handover folder (repo through WSL).
Read-only status at 00:37 (server clock): six services up 24 h, database at `0003`, 1.9 GiB
memory available, no rough trade yet (next decision 18 Nov 09:00 UTC).

**Damian's decisions (18 Nov 2026):**

- **Add-on h, the gate's recorded day:** a read-only pull. A sample of 16 Nov 2026 (every 10th
  minute, 236,268 option rows, plus that day's futures and indexes, trades, and every minute's
  open interest 12:00-18:00) was copied by `SELECT ... COPY TO STDOUT` over SSH into a scratch
  folder on the laptop at 00:43 UTC. Nothing on the server changed; no raw data is in the repo.
- **Add-on j, compatible schemas: yes, now.** Implemented as a check of each service's own
  tables (columns, types, NULLs, constraints) instead of the revision number
  (`optlab.market_data.schema`). A read-only comparison of the 16 live tables on 18 Nov (before
  any change): all 16 match the code's fingerprints, so the new recorder, export and paper
  service start on the live database before and after `0004`.

**Built** (tests first for every maths function):

1. `docs/conventions.md`: units, time, prices, Greeks, inverse-to-USDC conversion (valuation
   at the expiry's forward; BTC cash stays BTC).
2. `optlab.pricing.black76`: undiscounted Black-76 (Deribit's interest rate is 0 on every
   option), price, delta, gamma, vega per vol point, theta per day, inverse BTC premium.
3. `optlab.pricing.iv`: Newton from the Manaster-Koehler start, kept inside a shrinking bracket,
   Brent fallback; no-arbitrage bounds; flags (`iv_error` per price tick, `unreliable`).
4. `optlab.volatility.forward`: the dated future's mark, put-call parity (median of the 3
   nearest clean pairs) as fallback and cross-check.
5. `optlab.volatility.smile`, `surface`: spline smile per expiry (out-of-the-money mids,
   weighted by 1 / IV spread, GCV smoothing); buckets 10/25-delta puts and calls and ATM at 1,
   3, 7, 14, 30, 60, 90 days, in total variance; butterfly (Gatheral g) and calendar checks.
6. `optlab.volatility.features`, `realised`; worker `optlab-surface` (`worker`, `store`);
   migration `0004` (`vol_expiries`, `vol_surface` hypertables compressed after 7 days;
   `features`). Every minute is read through `MarketDataView(as_of=minute)`; a leakage test
   changes every row after t in a second database and checks the minute at t is identical.
7. `docs/formulas.md` and `docs/accounting.md`, each with a worked example; six questions for
   Damian between them.
8. The status page's "Volatility surface" tab: surface grid, live smiles (our fit, our quote
   mids and Deribit's marks), expiries with forward and checks, ATM IV against realised vol.

Also: the `surface` Compose service (384 MB, no port, no keys); scipy 1.18.1 added.

**Evidence so far:**

- **Golden tests pass**: 432 grid cases and the 21 Black-Scholes rows of `greeks_table.csv` from
  btc-vol-models at `4bb1cb1` (`tests/pricing/golden/btc_vol_models.json`, generated by
  `research/btc_vol_models_golden.py`); five-point finite differences to 1e-7; put-call parity;
  IV round trips; Brent and Newton agree.
- **Deribit's conventions, measured** on the 16 Nov sample: inverse premium = USD price /
  forward (to the 0.0001 BTC rounding; dividing by the index is off by up to 0.1 BTC); delta is
  N(d1), not premium-adjusted; vega and theta in USD for both books.
- **The comparison on 16 Nov** (144 minutes, both books, 235,670 quotes; generated at `4c0a907`;
  `research/deribit_comparison.md` now holds the 17 Nov gate run):

  | Check (expiries 1 day or more unless said) | BTC_USDC | BTC |
  | --- | --- | --- |
  | Engine delta, max difference | 0.000053 | 0.000047 |
  | Engine gamma, max (Deribit rounds to 0.00001) | 0.0000050 | 0.0000050 |
  | Engine vega, max (USD per vol point) | 0.017 | 0.029 |
  | Engine theta, expiries 7 days or more, max (USD a day) | 0.013 | 0.013 |
  | Engine IV from the mark price, 7-30 days, abs delta 0.05-0.50, 99th percentile (vol points) | 0.23 | 0.25 |
  | Surface IV against the mark, 7-30 days, abs delta 0.15-0.30: median / 99th percentile | 0.11 / 0.62 | 0.09 / 0.42 |
  | Surface delta against Deribit's, 7-30 days, abs delta 0.15-0.30, 99th percentile | 0.0033 | 0.0022 |
  | 0.20-delta short leg: different strike from Deribit's delta | 2 of 286 (0.7%), the neighbour | 5 of 576 (0.9%), the neighbour |
  | Forward, future against parity: median / 95th percentile | 1.5 / 4.4 bp | 1.3 / 4.0 bp |
  | Smile fits with butterfly arbitrage (7-30 days) | 25 of 1,018 (0) | 84 of 1,598 (0) |

  Outside these: within a day of expiry Deribit's delta and theta follow its settlement rules,
  and from 1 to about 4 days its theta isn't the instantaneous Black-76 theta; deep
  in-the-money (abs delta above 0.85) and far-wing IVs are dominated by price ticks.
- **Tolerances proposed** (add-on g; agreed by Damian on 18 Nov 2026). Engine, expiries 1 day or
  more: delta 0.0001; gamma 0.00001 per USD; vega 0.05 USD per vol point; theta 0.05 USD a day
  (7 days or more); IV from the mark price 0.5 vol point at the 99th percentile (7-30 days, abs
  delta 0.05-0.50). Surface, 7-30 days, abs delta 0.05-0.50: IV median 0.25 and 99th percentile
  1.0 vol point; delta 0.005 at the 99th percentile; the 0.20-delta leg differs from Deribit's
  pick in at most 2% of choices and only by one strike; forward against parity 10 bp at the
  95th percentile.
- **Open interest** (add-on e): it lags trades by up to hours and irregularly (see
  `docs/later.md`); no feature uses it.
- Tests: 1,499 pass on the laptop with the database (the 9 rclone tests skip there; CI runs
  them); ruff, ruff format and mypy (strict) clean.

**Deploy on recorder-1 (18 Nov 2026, server clock; Damian's "Yes, deploy")**, following
infra/server-setup.md section 17:

- *01:45, before.* All six services Up 25 h, 0 restarts; 1.9 GiB memory available; disk 27%;
  the 17 Nov backup succeeded at 00:31:51 and pinged OK; checkout clean at `9ccff8e`.
- *01:45:59-01:49:19.* `git pull --ff-only` to `3bed9c3`; image built (1.98 GB). Running
  services kept the old image. The server's remote still uses the old repo name through
  GitHub's redirect (not changed: open question).
- *01:49:35-01:51:56, backup.* `backups/pre-migration-20261118T0149Z.dump`, 470 MB, read back
  with `pg_restore --list`: 49 table-data entries. pg_dump warned about circular foreign keys
  on TimescaleDB's own catalog table `continuous_agg`, its standard warning, which matters only
  for data-only dumps. Disk 31% after.
- *01:52:13-01:52:17, migration.* `0003 -> 0004` in a one-off container (`--no-deps`):
  `vol_expiries` and `vol_surface` hypertables with compression, `features`. All six services
  kept running; 3,076 option rows in the two minutes around it.
- *01:52:17-01:52:24, dry run.* `optlab-surface --once --dry-run`: both books' surfaces from
  the live minute, nothing stored (BTC_USDC ATM 30 d 41.09%, RV30 44.29%, no arbitrage flags).
- *01:53:02, restart.* `infra/dc up -d --no-deps recorder jobs paper dashboard surface`, all
  started by 01:53:10. `db` and `health` untouched; the other project untouched.
- *After (01:56).* Seven services Up, 0 restarts. **No snapshot minute missed**: 1,538 option
  rows in each minute 01:48 to 01:56. The recorder logged "database at revision 0004; the
  recorder's tables are as expected". The surface worker stores every minute for both books
  (35 surface points and one feature row per book per minute); BTC_USDC ATM IV 30 d 41.13%
  against RV30 44.29% (30 returns, 27 of the 31 closes still delivery prices). Paper service:
  new run `20261118T015314Z-a97c69`, rule unchanged, 0 skipped. `jobs`: backup check pinged
  OK, nothing new. Memory: surface 114 MiB of 384, paper 87, recorder 69, `db` 583 MiB;
  2.2 GiB available.
- *Status page* (through the SSH tunnel, 01:56-01:58): the "Volatility surface" tab shows the
  metrics, the surface grid (the 1-day row extrapolated, as today's 08:00 expiry is under 12 h
  away), the 27 Nov smile (our fit, our quote mids and Deribit's marks; fit RMSE 0.15 vol
  points, butterfly and calendar ok), the expiries table and ATM IV against RV. Future against
  parity in the first stored minutes: an average 0.4 to 3.5 bp per expiry, at most 5.7 bp.

## Phase 2B · Gate check (17 Nov 2026)

Checked on 17 Nov 2026, 14:40 to 14:45 UTC (server clock), read-only: nothing on the server
was changed. Server checkout `9ccff8e`.

| Gate part | Result |
| --- | --- |
| Paper service runs 24 hours | **Pass.** 33 h 21 min without a restart, then back on its own after the server restart |
| The page shows its trades | **Not yet.** Three daily decisions, all "no trade" with the reason; no spread opened |
| Shortcut list exists | **Pass.** `docs/rough-version.md`, 28 numbered rows |

**Run record** (`rough_runs`):

| Run | From | To | Minutes processed | Skipped | Note |
| --- | --- | --- | --- | --- | --- |
| `20261115T153016Z-e04fee` | 15 Nov 15:30:16 | 17 Nov 00:51:08 | 2,002 | 0 | ended by the server power-off at 00:51:56 |
| `20261117T005252Z-aa05e3` | 17 Nov 00:52:52 | still running (14:42) | 830 | 1 | started by itself after the boot |

Docker restart count 0. Memory 94.6 MiB of its 512 MiB limit at 14:40. `rough_equity` has one
row per processed minute (2,831 rows, 15 Nov 15:30 to 17 Nov 14:42), all 100,000 USDC with no
open spread. Equity minutes match snapshot minutes each day (510/510, 1,440/1,440, 881/882; the
one difference is the skipped minute).

**Daily decisions** (`rough_decisions`):

| Day | Decided (UTC) | IV30 | RV30 | Index | 20-day average | Action | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 15 Nov | 15:30:16 (first start) | 41.47% | 43.98% | 64,600.74 | 75,478.20 | no trade | IV30 41.47% is not above RV30 43.98% |
| 16 Nov | 09:00:04 | 39.80% | 44.20% | 64,404.86 | 74,741.05 | no trade | IV30 39.80% is not above RV30 44.20% |
| 17 Nov | 09:00:04 | 41.35% | 44.23% | 64,276.68 | 73,927.29 | no trade | IV30 41.35% is not above RV30 44.23% |

Implied volatility has stayed 2.5 to 4.4 points below realised since the deploy, so the
volatility filter has blocked every day before any spread was looked at. `rough_spreads` and
`rough_fills` are empty, so there is no trade to work by hand.

**Gap report** (`optlab-data gap-report`, run 17 Nov at about 14:42 UTC):

| UTC day | Hours checked | Minutes recorded / expected | Option rows | Skipped minutes | Longest gap | Findings |
| --- | --- | --- | --- | --- | --- | --- |
| 15 Nov | 24/24 | 1440/1440 | 2,523,820 | 0 | 1 min | empty_book 1,878 |
| 16 Nov | 24/24 | 1440/1440 | 2,361,204 | 0 | 1 min | empty_book 2,679, stale_quote 1,344 |
| 17 Nov (to 14:00) | 14/14 | 839/840 | 1,309,733 | 2 | 2 min | empty_book 676, missing_snapshot 1, partial_snapshot 1, server_reboot 1, stale_index 2 |

Skipped periods since the deploy: 17 Nov 00:52 to 00:53 (missing_snapshot, server_reboot,
stale_index) and 17 Nov 11:06 to 11:07 (partial_snapshot). The 1,344 stale_quote findings on
16 Nov are quotes over 120 s old; they remove no minute. They go into the threshold re-check
on more data (Phase 5).

**Nightly backups:** both succeeded and pinged OK.

| Run (UTC) | Day | Rows | Export | Raw files | Dataset version |
| --- | --- | --- | --- | --- | --- |
| 16 Nov 00:32:03 | 15 Nov | 2,576,335 | 83.9 MB | 50, 311.6 MB | `d9ee47914f44…` |
| 17 Nov 00:32:17 | 16 Nov | 2,409,924 | 76.5 MB | 48, 331.7 MB | `6ec6a5c196fc…` |

**BTC_USDC 4 Dec expiry: still not listed.** Deribit's live list on 17 Nov 14:43 UTC: 18, 19,
20, 21 and 27 Nov, 25 Dec, 26 Mar. So from 20 Nov 08:00 to 25 Nov 08:00 UTC no BTC_USDC expiry
is 7 to 30 days away, and the rule makes no entry on those days.

**Damian's decision: option A** (his standing answer, 17 Nov 2026). The gate stays open until
the first rough trade shows on the page; Phase 3 starts meanwhile, because it is laptop work
that doesn't touch the paper service. The rules are not loosened. Phase 2B is marked passed
only when a live rough trade appears.

**Two Phase 2 jobs settled:**

- the R2 lifecycle rule was added on 15 Nov 2026 at 02:17 UTC (prefix `raw/`, delete 548 days
  after upload, enabled);
- the laptop sync is postponed to Phase 4, with a read-only R2 token and rclone 1.75.2 (the
  pinned version, not Ubuntu's apt package).

### The server restart on 17 Nov 2026

The server did **not** restart under the 04:00 policy. On 16 Nov at 06:57 unattended-upgrades
scheduled `shutdown -r 04:00` for 17 Nov, as expected. But at **00:51:56 UTC** on 17 Nov the
server was powered off from DigitalOcean (`systemd-logind`: "Power key pressed short", the
control panel's or API's power button) while another of Damian's projects was being set up
(see "Open questions"). It booted again at **00:52:32**. That boot applied the waiting libc6
update and cleared the scheduled 04:00 restart.

It is the first real test that everything comes back by itself after a restart:

| After-check | Result |
| --- | --- |
| All six services Up without help | Yes, all started at 00:52:45; Docker restart counts 0 |
| Recorder writing again | Yes: rows at 00:51 and from 00:53 (1,586 options a minute); 1,530 a minute at 14:40 |
| Snapshot minutes missed | One: 00:52 |
| Missing minute labelled | Yes: `missing_snapshot`, `server_reboot`, `stale_index` |
| `/var/run/reboot-required` | Gone |
| Scheduled shutdown | None |
| New `host_boots` row | Boot 00:52:32, first seen 00:52:54 |
| Paper service resumed | Yes, new run at 00:52:52 |
| `jobs` | Restarted, recorded the boot, backup check pinged OK |
| Restore timer | Active again, next run Tue 1 Dec 03:06 UTC |
| Kernel | Unchanged, 7.0.0-43 (the update was libc6 only) |
| healthchecks.io | No alert email (Damian, 17 Nov): the recorder was down about a minute, inside the 5-minute grace |

The 04:00 policy itself has still not fired; it will the next time an update needs a restart.

## Phase 2B · Rough end-to-end version (15 Nov 2026)

Everything from this step is labelled ROUGH. Its profit and loss is not a result.
`docs/rough-version.md` has the rule, the price-history source, the fees, and 28 shortcuts,
each with the phase that replaces it.

**Built** (commit `9ccff8e`, CI run 43729005076 green: lint, types, tests, Docker image,
full-history secret scan):

1. **The rough rule** (`optlab.strategy.rough`, `configs/strategies/rough.yaml`). Damian
   confirmed the definitions on 15 Nov.
   - *Filters.* 30-day ATM IV from BTC_USDC mark IVs: each expiry's IV at its forward,
     interpolated in total variance to 30 days. RV30: 30 daily log returns of the 08:00
     `btc_usdc` delivery price, sqrt(365 * mean r^2). IV must be strictly above RV. Trend: the
     `btc_usdc` index against the mean of the last 20 delivery prices.
   - *Spread.* Short leg |delta| 0.15 to 0.30, 7 to 30 days to expiry. The long leg is the
     nearest further-out strike 2% to 6% of the forward away.
   - *v1 checks* (Damian chose them for the rough version too): two-sided, not crossed,
     bid-ask at most 10% of mid, quotes under 2 minutes old, credit after fees at least 20% of
     the width.
   - *Choice.* The closest to 0.20 delta that passes every check. If the closest fails (for
     example on the minimum credit), the next one that passes is taken. If none passes, "no
     trade" is stored with the count of each failure. Both cases are tested.
   - *Exits.* Every minute: take profit at 50% of the entry credit, stop at 2 times, both on
     executable prices before fees; otherwise settlement at the delivery price.
   - *When.* Once a day from 09:00 UTC; on a start after 09:00 with no decision that day,
     straight away.
2. **Risk** (`optlab.risk.rough`, `configs/risk/rough.yaml`): size to a 2% maximum loss, cut to
   the quoted size and floored to 0.01 BTC; at most 5 open spreads, 2 per expiry, 10% total
   maximum loss.
3. **Fees** (`configs/fees/deribit.yaml`, `optlab.simulator.rough_fees`): Damian confirmed them on
   15 Nov 2026 (see below).
4. **Ledger** (`optlab.ledger.rough`): exact decimals in USDC. Entry sells the short leg at its
   bid and buys the long leg at its ask; the exit reverses that; settlement closes each leg at
   intrinsic value with one delivery fee per in-the-money leg. Equity is cash plus the open
   spreads at Deribit's mark.
5. **Paper service** `optlab-paper-rough` (`optlab.simulator.rough_paper`): the `paper` Compose
   service (512 MB, `restart: unless-stopped`, no port, no keys).
   - It processes each snapshot minute once, through `MarketDataView(as_of=minute)`.
   - It skips flagged minutes: known skip periods, plus live checks for a partial snapshot and
     a missing or stale index.
   - Each entry or close is stored in one transaction, so a restart never enters twice.
   - It imports no exchange client, and a test runs a full step with every Python network
     connection refused.
   - Migration `0003` adds the `rough_*` tables and `host_boots`.
6. **Page**: the status page's "Paper trading (ROUGH)" tab: equity chart, daily decisions with
   reasons, open and closed spreads, every paper fill, and the service's last run.
7. **Reboot policy**: unattended-upgrades restarts at 04:00 UTC only when an update needs it
   (`infra/apt/52optlab-reboot`, infra/server-setup.md section 6). The `jobs` service records
   each boot (`host_boots`), and the hourly checks label the gap as `server_reboot` (checks
   version 2).

Tests: 267, all passing on the laptop with none skipped (the database, plus rclone copied from
the pinned image). They include 63 for the rough version, with hand-worked golden values for
IV30, RV30, the fees, fills and settlement. The recorder's `SCHEMA_REVISION` is now `0003`.

**Fees (Damian confirmed the numbers on 15 Nov 2026).** From Deribit's fee page
(https://support.deribit.com/hc/en-us/articles/25944746248989-Fees, updated 11 Nov 2026 09:46
UTC), Standard tier, effective from 10 Nov 2026 12:00 UTC:

| | Linear (BTC_USDC) | Inverse (BTC) |
| --- | --- | --- |
| Trading, maker / taker | 0.03% / 0.03% of the index price, in USDC | 0.03% / 0.03% (0.0003 BTC per contract), in BTC |
| Cap | 12.5% of the premium | 12.5% of the premium |
| Daily (non-Friday) expiries | maker 0 until 31 Jan 2027; taker 0.03% | no change |
| Delivery, in-the-money legs | 0.015% of the delivery price, at most 12.5% of the option's value | the same |
| Daily options' delivery | 0 | 0 |

The page also lists these, none used by the rough version: the combo discount (the cheaper
direction is free), block trades at 0.03%, and VIP1 from 100k USDC equity (0.025%, cap
10.42%). Damian kept Standard. On delivery fees, Damian confirmed (Deribit's announcement
https://insights.deribit.com/?p=113365 and the Linear USDC Options article) that each expiring
in-the-money option pays its delivery fee before any netting, and the future it becomes pays
none. A spread with both legs in the money therefore pays one fee per in-the-money leg, as
built.

**Price history.** RV30 and the trend use the `btc_usdc` daily delivery prices from Deribit's
free public `public/get_delivery_prices`. The recorder already stores them in
`delivery_prices`: it backfilled 30 days (15 Oct to 13 Nov) at its first start and adds each
day since. Nothing new was fetched.

**Deploy on recorder-1 (15 Nov 2026, server clock).**

- *15:20–15:24.* `git pull`, then the image built on the server. Running services kept their
  old image.
- *15:24–15:25, backup before the migration.* `backups/pre-migration-20261115T1524Z.dump`,
  212 MB, read back with `pg_restore --list`: 37 table-data entries. Disk 21% used.
- *15:25, migration.* `0002 -> 0003` in a one-off container with `--no-deps`. All five
  services kept running.
- *15:30:02, restart.* `infra/dc up -d --no-deps recorder jobs dashboard paper`: the recorder
  because of its schema check, `jobs` for the reboot label, `dashboard` for the new tab, and
  `paper` new. `db` and `health` were untouched.
- *Snapshots.* None missed: 1,746 option rows in each minute 15:26 to 15:31.
- *Memory.* Paper 114 MiB of 512; recorder 87 MiB; dashboard 54 MiB; `db` 1.11 of 1.5 GiB.
- *The 00:30 backup* was far away, so `jobs` was not paused. Its restart's catch-up run backed
  up nothing new (ping OK) and recorded the current boot (9 Nov 22:21:26 UTC).
- *Reboot policy* installed (`/etc/apt/apt.conf.d/52optlab-reboot`); `apt-config dump` shows
  all three settings.
- *Page.* Rendered headless inside the `dashboard` container: both tabs, today's decision, no
  error.

**First decision (15 Nov, at 15:30 UTC, the service's first start):** no trade, "IV30 41.47% is
not above RV30 43.98%". BTC was at 64,601, against a 20-day average of 75,478. Today's 08:00
delivery price added a large daily fall to the 30-day window. The next decision is 16 Nov
09:00 UTC. As Damian asked, if no trade has happened by the gate check, the report says so and
the rules stay as they are.

**A restart is already waiting.** `libc6` was updated on 13 Nov 06:20 and set
`/var/run/reboot-required`. unattended-upgrades 2.12 schedules the restart after every completed
run while that file exists, even a run that installs nothing (checked in its code). The next run
(about 06:57 UTC on 16 Nov) will therefore schedule `shutdown -r 04:00`, and the server will
restart by itself at **04:00 UTC on Tue 17 Nov**. That is after the gate check. Docker and every
service start again on their own; this was not tested by rebooting, as Damian asked. To cancel
it once scheduled: `sudo shutdown -c`.

**BTC_USDC 4 Dec expiry:** not listed. Deribit's live list on 15 Nov 15:35 UTC: 16, 17, 18, 19
(new), 20 and 27 Nov, 25 Dec and 26 Mar. Unless Deribit lists one, from 20 Nov 08:00 to 25 Nov
08:00 UTC no BTC_USDC expiry is 7 to 30 days away, and the rule makes no entry on those days
(tested).

**Laptop clock.** WSL's clock fell about 10 hours behind the server's during this session,
probably after the laptop slept. The commit's timestamp (about 05:3x UTC) is that clock. The
server's chrony clock is right, and every time in this log is the server's.

## Damian's Phase 2 decisions (15 Nov 2026)

- **Book:** BTC_USDC (linear) is the main book. The inverse book keeps being recorded as the
  fallback. Phase 2B uses BTC_USDC.
- **No BTC_USDC expiry 7 to 30 days away:** no entry that day, and no switch to inverse.
- **Data-quality thresholds** (`configs/data_quality.yaml`): confirmed as proposed, to be
  re-checked on a few weeks of data.
- **Data-quality flags and entries:**
  - a flag on one option blocks only spreads using that option;
  - a flag on an expiry's future blocks every spread on that expiry;
  - global flags (missing or partial snapshot, stale index) block all entries.

  The rough version may simply skip flagged minutes (a listed shortcut); Phase 5 builds the
  full rule.
- **Minimum credit:** keep 20% of the width for v1. Alternatives are tested in Phase 5 on
  validation data only.
- **Rough version:** the v1 entry checks and limits apply. Among short legs with |delta| 0.15
  to 0.30, take the one closest to 0.20 that passes every check. Every daily decision shows on
  the page with its reason. The rules are not loosened to force a trade.
- **Reboot policy:** automatic restart at 04:00 UTC, only when an update needs one.

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

1. Phase 4 (backtest engine): the build may start any time; the gate runs on the week 14-20
   Nov from Sat 21 Nov 2026. First: the laptop sync (rclone 1.75.2) with the read-only R2
   token Damian created on 18 Nov (he pastes it into the laptop's `.env` by hand). The margin
   model is built in Phase 4 (decided 18 Nov).
2. Phase 2B: every status check looks for the first rough trade (option A).
4. Dates: 20 Nov 08:00 UTC the 20 Nov expiry settles (no BTC_USDC entries possible 20 Nov 08:00
   to 25 Nov 08:00 unless Deribit lists 4 Dec); 21 Nov the first full recorded week (14 to 20
   Nov) is complete, which Phase 4 needs; 1 Dec about 03:06 UTC the first monthly restore test.

## Open questions

- **A second project on recorder-1 (found 17 Nov 2026; Damian decided 17 Nov: keep it there).**
  Between 00:44 and 01:18 UTC on 17 Nov,
  the setup of another of Damian's projects (a password-protected web mirror of its dashboard)
  was run against this server, from keys made on Damian's laptop at 00:14 UTC:
  - the server was powered off and on from DigitalOcean (00:51:56 to 00:52:32; one snapshot
    minute lost, labelled);
  - a second admin key, `btc-droplet-admin`, was added to `deploy`, and a new user `btcsync`
    (password locked, one key, upload target `/srv/btc-data`) was created;
  - files in `/opt/btc-strategy`, and the Compose project `btc-dashboard`: an app container
    and Caddy, which **publishes ports 80 and 443 to the internet**. Port 80 answers from the
    laptop, so the cloud firewall no longer allows SSH only.

  Nothing of this project was touched, and the other project's containers use about 127 MiB.
  But it breaks three of this project's rules: the server firewall is SSH-only, nothing
  listens on a public port, and the recorder's server changes only with a reason and Damian's
  yes. That project's own guide plans a separate small Droplet.

  **Decision (Damian, 17 Nov 2026): keep it on recorder-1.** Recorded as an agreed exception:
  - recorder-1 now hosts two projects. Ports 80 and 443 are open to the internet for the
    other project's Caddy only. This project's own rule is unchanged: none of its containers
    publishes a port (still test-enforced), and the status page stays on 127.0.0.1. Phase 6
    still publishes through Cloudflare Tunnel.
  - Claude Code doesn't change, restart or remove anything of the other project
    (`btc-dashboard`, `/opt/btc-strategy`, `/srv/btc-data`, user `btcsync`), and doesn't stop
    it to free memory without Damian's yes.
  - Its memory counts in this project's budget: about 127 MiB of the server's 3.8 GiB (1.9 GiB
    available on 17 Nov). Phase 3's new worker checks free memory first.
  - Work on the other project that can stop or restart the server (DigitalOcean power
    actions, resizes, firewall changes) costs recorder minutes. Better done outside 07:55 to
    08:35 UTC, 00:30 and 09:00, as for this project's own deploys.
  - The status check reports both projects' containers, so anything new shows up.
- **The GitHub repo is now public and renamed `btc-options-test` (found 17 Nov 2026 at 14:46
  UTC).** It was created private on 27 Oct, and the plan is private until v1.0. GitHub last
  updated the repo at 14:35:56 UTC on 17 Nov; pushes to the old name still work through
  GitHub's redirect. A scan of all 14 commits found no server address, no Cloudflare account
  ID, no R2 endpoint and no ping URL (only the secret scanner's keyword rules and made-up test
  values), and CI's gitleaks full-history scan is green.

  **Decision (Damian, 17 Nov 2026): keep it public**, under the new name. What follows from it:
  - every push is public at once, so the rules on secrets, the server address, the R2
    endpoint and the Cloudflare account ID now apply to each commit, not just at release.
    Before each push: the pre-commit hooks (gitleaks), and a search of the diff for those
    values from the command line;
  - Phase 9's "make the repo public" step is already done; its full-history scan and README
    still come first in that phase;
  - until the MIT licence is added (Phase 9), the code is visible but not licensed for reuse;
  - the laptop's remote points at the new name. The server's checkout still uses the old
    name through GitHub's redirect; switching it is a server change, done at the next
    deploy with Damian's yes.
- **Open interest delay.** Deribit's ticker docs say `open_interest` is reported with a delay.
  Stored as given; Phase 3 should check it before using it.
- JEV details (inputs, outputs, determinism, speed and cost per call, training dates, whether it
  can be self-hosted). Needed by week 4.
- The DigitalOcean metrics agent (`do-agent`) is not installed on recorder-1, although the brief
  says monitoring is on. Install it from the Droplet's Graphs tab if you want the graphs (see
  `docs/later.md`).

Closed on 17 Nov 2026:

- **The second project on recorder-1:** kept there as an agreed exception (see the open
  question above for the terms).
- **The GitHub repo:** stays public as `btc-options-test` (terms above).
- **Nothing traded by the 2B gate:** option A (gate open until the first rough trade; Phase 3
  meanwhile).
- **The pending libc6 restart:** applied by the 17 Nov 00:52 boot; every service came back by
  itself.
- **R2 lifecycle rule:** added 15 Nov 02:17 UTC (`raw/`, 548 days, enabled).
- **Laptop sync:** Phase 4, read-only R2 token, rclone 1.75.2.

Closed on 15 Nov 2026 (Damian's decisions, above):

- **Book:** BTC_USDC (linear), with the inverse book recorded as the fallback.
- **No BTC_USDC expiry 7 to 30 days away:** no entry that day; no switch to inverse.
- **Data-quality thresholds:** confirmed; to be re-checked on a few weeks of data.
- **Data-quality flags and entries:** an option flag blocks only spreads using that option; a
  future's flag blocks its expiry; global flags block all entries.
- **Minimum credit:** 20% of the width for v1; alternatives in Phase 5 on validation data only.
- **Fees for Phase 2B:** confirmed from Deribit's fee page (Standard tier), delivery fee once per
  in-the-money leg.
- **Reboot policy for kernel updates:** automatic restart at 04:00 UTC when an update needs one.

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
