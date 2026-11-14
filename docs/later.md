# Later

New ideas go here, not into the current phase. Each entry says where it came from and which phase
could pick it up.

- **Server setup as a script** (Phase 0). Turn `infra/server-setup.md` into an idempotent script,
  so a rebuild is one command after the Droplet exists. Phase 9 at the latest.
- **Reboot policy for kernel updates** (Phase 0). Security updates install automatically, but
  nothing reboots on its own, so new kernels wait. Options: a weekly scheduled reboot at a quiet
  hour (the recorder restarts by itself in under 5 minutes), or a healthchecks alert when
  `/var/run/reboot-required` exists. Phase 1 or 2, alongside the disk check.
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
