# Later

New ideas go here, not into the current phase. Each entry says where it came from and which phase
could pick it up.

- **Server setup as a script** (Phase 0). Turn `infra/server-setup.md` into an idempotent script,
  so a rebuild is one command after the Droplet exists. Phase 9 at the latest.
- **DigitalOcean metrics agent** (Phase 0). `do-agent` wasn't installed on recorder-1 even though
  monitoring was ticked. Damian may install it from the Droplet's Graphs tab; it doesn't affect the
  platform.
- **Backfill trades after a reconnect** (Phase 1). Trades sent while a connection was down are
  lost from the live feed, but `public/get_last_trades_by_currency_and_time` can fetch them;
  `option_trades` already skips duplicates by `trade_id`. Phase 2, with the gap report.
- **Instrument events instead of polling** (Phase 1). Deribit's `instrument.state.{kind}.{currency}`
  and `instrument.creation.{kind}.{currency}` channels announce listings and expiries as they
  happen; discovery could run on those as well as every 10 minutes. Any phase.
- **Smaller raw files** (Phase 1). Sorting each Parquet row group by instrument, or delta-encoding
  the timestamps, would likely shrink the raw stream a lot. Worth doing if the day-one volume
  is large.
- **Open interest timing** (Phase 1). Deribit's ticker docs say `open_interest` is reported with
  a delay. Phase 3 should check it before any feature relies on it.
- **Short reconnects without a hole** (Phase 1 gate). A connection that drops seconds before a
  minute boundary leaves its instruments out of that snapshot (seen once on day one: 21:11 on
  13 Nov). The snapshot could instead use tickers received only seconds before the drop, flagged
  as such. Only worth it if the Phase 2 gap report shows it happening often.
- **Read-only database role for the dashboard** (Phase 2). The status page uses `DATABASE_URL`
  with every session set read-only. Before the site goes public (Phase 6), give it its own
  PostgreSQL role with SELECT only, and its own URL in `.env`.
- **Tardis first-of-month importer** (Phase 2, item 8, left out for time). Read Tardis's free
  `options_chain` files for the first day of each month into the same `exports/v1` layout, so
  pricing and surface code can be checked in other market conditions. Phase 3 or later.
- **Smaller exports** (Phase 2). The nightly export sorts by instrument and time with zstd
  level 9. If R2 growth matters later, delta-encoding timestamps or dropping columns that never
  change within a day would shrink it further.
- ~~**Recorder accepts compatible schema revisions**~~ (Phase 2B, Damian). Done in Phase 3
  (18 Nov 2026), as a check of each service's own tables rather than a list of revisions
  (`optlab.market_data.schema`, infra/server-setup.md section 17).
- **Arbitrage-free fit for long expiries** (Phase 3). The spline smile shows small butterfly
  violations in some expiries 30 days or more out (mostly 4 to 10 months, inverse book; none 7 to
  30 days on 16 Nov). They are flagged and stored. An SVI-type fit with no-arbitrage
  constraints would remove them if a later phase prices long expiries. Phase 10 at the latest.
- **Deribit's near-expiry theta** (Phase 3). Within about 4 days of expiry Deribit's theta stops
  being the instantaneous Black-76 theta (close to the one-day change in value in the last day).
  Ours stays Black-76. Phase 5's Greek attribution near expiry should say which it uses.
- **Open interest is slow** (Phase 3, add-on e). On 16 Nov the ticker's open interest changed in
  under 1% of the minutes after a trade, a median 56 minutes later (90th percentile 167). No
  minute feature uses it. A daily open-interest feature would take it at a fixed time and treat
  it as hours old.
- **Rebuild the surface history** (Phase 3). The worker starts from the live minute; the surface
  for 13 to 18 Nov (and any minute missed during a restart) can be rebuilt from the exported
  days with the same code (`compute_minute` over `ParquetSource`). Phase 4, with the laptop sync.
- **Combo fills and the combo fee discount** (Phase 2B). Deribit charges nothing on the cheaper
  direction of an option combo with buys and sells; the rough version fills leg by leg and pays
  both. Phase 4 (fees) and Phase 8 (combo orders on testnet).
