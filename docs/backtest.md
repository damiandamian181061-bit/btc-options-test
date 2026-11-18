# Backtest engine (Phase 4)

How a backtest turns recorded minutes into fills, books and equity, and every simplification
it makes. The accounting is `docs/accounting.md` (signed off 18 Nov 2026); the formulas are
`docs/formulas.md`.

## Running one

Backtests run on the laptop, from days synced from R2 (read-only token, rclone 1.75.2):

```bash
uv run optlab-data sync --from 2026-11-14 --to 2026-11-20   # into data/exports/v1
uv run optlab-backtest run configs/backtests/smoke.yaml      # run, save, log to MLflow
uv run optlab-backtest run CONFIG --no-save --no-mlflow      # summary only
uv run python research/phase4_gate.py                        # the Phase 4 gate checks
```

- **Config:** a YAML file in `configs/backtests/` loaded into `BacktestConfig`: the period
  (UTC, end exclusive), the strategy and its parameters, the risk limits
  (`configs/risk/<name>.yaml`), a fee multiplier (1; Phase 5 runs 0.5 to 3) and a seed.
- **Run record:** every run saves its whole config and its hash, the git commit (and whether
  the checkout had uncommitted changes), the dataset hash (SHA-256 over each exported day's
  dataset version, from the manifests) and the seed, plus a **result hash**: SHA-256 over every
  order, fill, position change, equity minute, risk event and group, in a canonical form. Two
  runs with the same config, code and data give the same result hash.
- **Results tables** (migration `0005`): `backtest_runs`, `backtest_orders`, `backtest_fills`,
  `backtest_positions`, `backtest_equity`, `backtest_groups` and `risk_events`, in a local
  PostgreSQL named by `OPTLAB_RESULTS_DATABASE_URL` (`infra/README.md`). Money is `numeric`.
- **MLflow:** each run logs its parameters, summary metrics, hashes and config file to
  `mlruns/` (git-ignored). The UI, when wanted, runs separately and only on the laptop's
  loopback address: `uvx --from mlflow mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db
  --host 127.0.0.1`.

## The event loop

`optlab.simulator.engine.Backtest`, for each snapshot minute t in the period:

1. **Market at t**, read only through `MarketDataView(source, as_of=t)`: the BTC_USDC option
   chain (Deribit's quotes, marks and Greeks as recorded), the indexes, futures and
   perpetuals, the instruments listed by t, and the delivery prices recorded by t.
2. **Settlement** of options whose expiry has passed, once Deribit's delivery price is known
   (recorded at 08:01). Until then an expired option is valued at its last mark.
3. **Funding** on open perpetuals for the time since the last minute, at the rate seen then.
4. **Risk monitors:** peak equity, the day's starting equity (cut at 00:00 UTC), maintenance
   margin.
5. **Decisions:** unless the hourly data checks marked the minute to skip, the strategy gets
   the market and a read-only portfolio and returns tickets. Each ticket's fill is worked out,
   checked by the risk gateway, and booked only if approved.
6. **Equity** at Deribit's mark, with the liquidation value (longs at the bid, shorts at the
   ask) alongside; the reconciliation check (below); one equity row.

Every recorded minute is processed, including the first minute after a gap, so a take profit
or stop is never skipped because the service was down (rough shortcut 14). Strategies see only
`MarketState` and `PortfolioView` and return `Ticket`s (`optlab.domain.orders`); they never see
the books, the fill simulator or the gateway.

## Fills (`optlab.simulator.fills`)

- A buy fills at the ask, a sell at the bid, each leg at its own price (no combo order).
- At most the size shown at the top of the book fills. The snapshot has no deeper levels, so
  the rest of a larger ticket is dropped and the order is recorded as `partial`; a strategy
  that wants the rest sends it again later (rough shortcut 15).
- The legs of a spread fill the same amount, the smallest any leg can take: a spread is never
  left with one leg.
- Amounts are floored to the instrument's minimum trade amount (0.01 BTC for BTC_USDC
  options).
- A leg with no price on its side, or no size, leaves the ticket unfilled.

**Simplifications:** no latency (the fill is at the same minute's quote), no slippage beyond
the bid-ask spread, no market impact, no queue position. The fill's mid and mark are recorded,
so Phase 5 can measure the cost of crossing the spread.

## Fees (`optlab.simulator.fees`, `configs/fees/deribit.yaml`)

Deribit's fee page, re-read on 18 Nov 2026 (unchanged since 11 Nov 2026 09:46 UTC), Standard
tier, taker on every fill:

- options: `min(0.03% x index, 12.5% x price) x amount` per leg; both legs of a spread pay
  (no combo discount: Damian, 18 Nov 2026);
- delivery: `min(0.015% x delivery price, 12.5% x intrinsic value) x amount`, once per
  in-the-money option; daily (non-Friday) expiries pay none;
- the BTC_USDC perpetual: 0.035% of the traded notional.

**Not modelled:** the combo discount (Phase 8 measures it on testnet), VIP tiers (paper
capital of 100,000 USDC would meet VIP1's 100k USDC equity test, but Standard is the
conservative choice), block trades, liquidation and collateral fees, and Deribit's rounding of
fees (shortcut 17).

## Settlement

At Deribit's delivery price P (the 07:30-08:00 UTC average of the index), in two steps
(`docs/accounting.md`): an in-the-money option becomes a position in its expiry's future at the
strike and is closed at its intrinsic value into `settlement:<future>`; the future then
cash-settles at P, leaving that account at zero. The payoff equals the intrinsic value exactly
(a test proves it for calls and puts, long and short); one delivery fee per in-the-money
option, the future pays none (shortcuts 19 and 22). Out-of-the-money options expire worthless
with no fee. The expiry's future is read from Deribit's instrument metadata.

## Perpetuals and funding

For the hedged benchmarks of Set B. A perpetual trade moves no premium: only its fee is paid at
once; its entry notional is kept with the position, unrealised P&L is `size x mark - entry
notional`, and a close books the realised P&L to `pnl:hedge`.

Funding follows Deribit's "Funding Specifications" (read 18 Nov 2026): premium rate = (mark -
index) / index; funding rate = the premium beyond a +/-0.025% damper, capped at +/-5% for the
BTC_USDC perpetual; payment = rate x size x index x time held / 8 hours, longs paying when it is
positive.

**Simplification:** Deribit accrues funding continuously from every change of mark and index;
the backtest samples them once a minute and applies each minute's rate to the next minute.
Deribit moves funding into the cash balance at its 08:00 daily settlement; the backtest books it
to cash at once (equity is the same either way).

## The double-entry ledger (`optlab.ledger`)

`Journal` holds balanced entries in exact decimals; `Books` applies the booking rules of
`docs/accounting.md`. Positions are kept per instrument, as the exchange does; position groups
(`optlab.simulator.groups`) keep each trade's own legs, entry values and fees for the gateway
and for reports.

**Reconciliation** at every minute: change in equity = option P&L (realised and unrealised) +
hedge P&L - fees + funding, exactly, and the journal balances. The engine stops with an error
if either fails (rough shortcut 20).

The only roundings are in the results of divisions: the share of an entry value released by a
partial close, and funding. They are rounded to 1e-12 USDC before they are booked, so every sum
stays exact; the last close of a position releases exactly what is left.

Open positions are valued at Deribit's mark (Damian's choice, 18 Nov 2026). A position whose
option has no mark at t is valued at its last mark and counted in `stale_marks`; the equity row
is still written (rough shortcut 21).

## The risk gateway (`optlab.risk.gateway`, `configs/risk/v1.yaml`)

Every ticket passes the gateway before anything is booked; it checks the exact fill the
simulator worked out. An opening ticket is rejected, with one `risk_events` row per broken rule,
if:

| Rule | Limit (v1) |
| --- | --- |
| Kill switch | 10% drawdown from peak equity: no new trades until reviewed |
| Daily loss limit | a 3% loss within the UTC day (cut at 00:00): no new trades that day |
| Maintenance margin | margin balance below maintenance margin: no new trades |
| Maximum loss of the group | at most 2% of equity |
| Sum of open groups' maximum losses | at most 10% of equity |
| Open groups | at most 5, and at most 2 per expiry |
| Portfolio delta | net delta x index at most 20% of equity, either way |
| Portfolio vega | at most 0.5% of equity per vol point, either way |
| Initial margin | at most the margin balance (Deribit Standard Margin, below) |
| Short options | at most 1,000,000 USDC (short BTC x index), Deribit's Standard Margin limit |

A closing ticket only has to reduce its own group, and is never blocked by a limit, so exits
always work. The monitors log a `triggered` event once when they fire (and `cleared` for
margin). Delta and vega use Deribit's recorded Greeks (Phase 3 checked them against ours).

**Maximum loss of a group:** the worst P&L at expiry over every delivery price (piecewise
linear, so the worst is at a strike, at zero, or unbounded), less its entry value, plus the
fees it paid and the fees to close every leg at the current index (the 0.03% taker rate, which
is above the delivery fee rate unless the price doubles). This includes exit and delivery fees,
which the rough version left out (shortcut 23). A group holding a perpetual, net short calls,
or a short option that expires after another of its legs has no bounded maximum loss and is
refused while the loss limits are on.

### Margin (Deribit Standard Margin, decided 18 Nov 2026)

Damian chose Standard Margin: what a new account gets, and the most conservative, because a
long option gives no offset to a short one. From Deribit's "Linear USDC Options" page (BTC_USDC,
updated 18 Nov 2026 07:14 UTC), per BTC:

- short call IM `max(0.15 - OTM / F, 0.10) x index + mark`, MM `0.075 x index + mark`;
- short put IM `max((0.15 - OTM / F) x index, 0.10 x strike) + mark`, MM
  `0.075 x min(index, strike) + mark`;
- long options: the premium, paid in full.

F is the expiry future's mark (Deribit's `underlying_price` for the option). The margin balance
is cash plus the perpetuals' unrealised P&L. The BTC_USDC perpetual uses its smallest-size tier
(IM 2%, MM 1.33% of notional, up to 88.45 BTC; larger is refused).

**Simplifications:** no open-order margin (orders fill or vanish within the minute); no
"projected" margin in the half hour before an expiry; no simulated liquidation (a maintenance
shortfall stops new trades and is logged). With 100,000 USDC and v1's 10% total-loss limit, five
full-size spreads need roughly 40,000 USDC of initial margin, so the guard should rarely bind.
Phase 8 compares this estimate with what testnet charges.

## What is not built yet

- The inverse book (BTC-denominated premiums): the main book is BTC_USDC; the engine refuses
  another currency.
- Strategies: Phase 4 has only `smoke` (one ATM call held to expiry), `no_trade` and `exercise`
  (random trades for the gate tests, results meaningless). The v1 rules, benchmarks, metrics and
  attribution are Phase 5.
