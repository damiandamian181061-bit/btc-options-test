# Rough end-to-end version (Phase 2B)

**Everything here is ROUGH.** It runs the whole chain early: recorded quotes, a daily rule,
paper fills, a ledger and a page. It is internal paper trading on our own recorded quotes:
there is no Deribit account, no API key, and no order goes anywhere. `LIVE_TRADING` stays false.
Its profit and loss is **not a result**. No number from it counts until the Phase 4 fill, fee and
accounting tests pass. Each shortcut below names the phase that replaces it.

Gate (Phase 2B): the paper service has run for 24 hours, the status page shows its trades, and
this list exists.

## What runs

- **Service:** `paper` in `infra/compose.yaml` (`optlab-paper-rough`, 512 MB, `restart:
  unless-stopped`, no port, no keys). Every 10 seconds it looks for a new snapshot minute and
  processes it once.
- **Page:** the status page's "Paper trading (ROUGH)" tab (server's 127.0.0.1:8501). It shows
  every daily decision with its reason ("no trade" included), open and closed spreads, every
  paper fill, and equity.
- **Tables** (migration `0003`): `rough_runs`, `rough_decisions`, `rough_spreads`, `rough_fills`
  and `rough_equity`, with exact `numeric` money in USDC.

| Part | Code | Config | Tests |
| --- | --- | --- | --- |
| IV30, RV30, trend average | `optlab/volatility/rough.py` | `configs/strategies/rough.yaml` | `tests/rough/test_volatility.py` |
| Rule, candidates, exits | `optlab/strategy/rough.py` | `configs/strategies/rough.yaml` | `tests/rough/test_strategy.py` |
| Sizing and limits | `optlab/risk/rough.py` | `configs/risk/rough.yaml` | `tests/rough/test_risk.py` |
| Fees | `optlab/simulator/rough_fees.py` | `configs/fees/deribit.yaml` | `tests/rough/test_fees.py` |
| Fills, settlement, cash | `optlab/ledger/rough.py` | | `tests/rough/test_ledger.py` |
| Service, storage | `optlab/simulator/rough_paper.py`, `rough_store.py` | | `tests/rough/test_paper.py` |

The rough modules import no exchange client, no `optlab.execution` and no HTTP or websocket
library. A test checks this, and a second test runs a full step with every Python network
connection refused. Only the database is reached.

## The rule (confirmed by Damian, 15 Nov 2026)

On the BTC_USDC book, with Deribit's own Greeks and mark IVs, once a UTC day at the first usable
snapshot minute from 09:00 UTC (after the 08:00 expiry and delivery price). On a start after
09:00 with no decision yet that day, it decides straight away.

1. **Volatility filter:** 30-day ATM IV strictly above 30-day realised vol.
2. **Trend:** bull put spread if the `btc_usdc` index is above the mean of the last 20 daily
   delivery prices, bear call spread if below, no trade if equal.
3. **Expiry:** 7 to 30 days away. If no BTC_USDC expiry is in that window, no entry that day.
   There is no switch to the inverse book (Damian, 15 Nov).
4. **Candidates:** every short leg with |delta| 0.15 to 0.30. Its long leg is the nearest
   further-out strike whose distance is 2% to 6% of the forward.
5. **v1 checks:**
   - both legs two-sided and not crossed;
   - bid-ask at most 10% of mid;
   - quotes under 2 minutes old;
   - credit after fees at least 20% of the width.
6. **Choice:** among the candidates passing every check, the |delta| closest to 0.20. Ties go to
   the nearer expiry, then the lower strike. If the closest one fails, for example on the
   minimum credit, the next closest that passes is taken. If none passes, the day is "no trade"
   with the count of each failure.
7. **Size and limits** (risk gateway):
   - size so the maximum loss (width minus credit after entry fees) is at most 2% of equity;
   - cut the size to the quoted size (short bid, long ask) and floor it to 0.01 BTC;
   - at most 5 open spreads, at most 2 per expiry;
   - total maximum loss at most 10% of equity.

   A candidate the limits refuse passes to the next one.
8. **Exits, every minute:**
   - take profit when the executable buy-back (short ask minus long bid) is at most 50% of the
     entry credit (short bid minus long ask), both before fees;
   - stop when it is at least 2 times the entry credit;
   - otherwise settle at the `btc_usdc` delivery price once the recorder has it (08:01).
9. **Thesis** at entry, for example "BTC stays below 70,000 until expiry on 27 Nov 2026 08:00
   UTC; buy back at half the credit". At exit: met for a take profit, not met for a stop, and
   for a settlement, met if the short leg expired worthless.

### Price history (source)

Realised vol and the trend need daily BTC prices from before recording began (13 Nov). They are
the **`btc_usdc` daily delivery prices** from Deribit's free public endpoint
`public/get_delivery_prices`, which needs no key.

- The recorder already fetches them. At its first start (13 Nov 16:32 UTC) it backfilled the
  last 30, from 15 Oct to 13 Nov. Since then it adds each day's price at 08:01 UTC.
- They are stored in the `delivery_prices` table (exact `numeric`), on the server and in the
  nightly R2 export, never in git.
- The paper service reads them through `MarketDataView`, so only prices recorded by the decision
  time count.
- Nothing new was fetched for this phase. On 15 Nov there were 31 consecutive days (15 Oct to
  14 Nov), plus 15 Nov from 08:01.

### Fees

`configs/fees/deribit.yaml`, from Deribit's fee page
(https://support.deribit.com/hc/en-us/articles/25944746248989-Fees, updated 11 Nov 2026, read
and confirmed by Damian on 15 Nov 2026), effective from 10 Nov 2026 12:00 UTC, Standard tier:

- **Trading:** 0.03% of the index price per leg, capped at 12.5% of the premium. Maker and taker
  rates are the same; paper fills are taker.
- **Linear daily options:** non-Friday BTC_USDC expiries have a 0 maker fee until 31 Jan 2027.
  This doesn't matter here: paper fills are taker, and 7-30 day expiries are Fridays.
- **Delivery:** 0.015% of the delivery price on in-the-money legs, capped at 12.5% of the
  option's value. Daily options pay none.
- **Linear and inverse:** the same rates; only the currency differs (USDC or BTC).

## Shortcuts, and what replaces each

| # | Shortcut | Why it matters | Replaced in |
| --- | --- | --- | --- |
| 1 | ATM IV is Deribit's mark IV at the forward, linear in strike between the two listed strikes around it (call and put averaged). No smile fit, no IV of our own. | Steep skew makes the strike interpolation crude; Deribit's marks are not checked. | Phase 3: IV solver, spline smile, surface buckets |
| 2 | 30-day IV is total variance interpolated between the two BTC_USDC expiries around 30 days, with nothing outside them. Inverse expiries are not used. | With few USDC expiries the bracket can be wide (12 and 40 days on 15 Nov). | Phase 3 surface |
| 3 | The forward is the median of Deribit's `underlying_price` across the expiry's options. | Not our own forward from the future or put-call parity. | Phase 3 forwards |
| 4 | RV30 is close to close on the 08:00 `btc_usdc` delivery prices (each a 30-minute average of the index): 30 returns, zero mean, times sqrt(365). It needs 31 consecutive days. | Averaged fixings slightly understate vol; one missing day means no decision. | Phase 3 features (realised vol from our own data) |
| 5 | Trend compares the index at the decision minute with the mean of the last 20 delivery prices. | The brief's "20-day average" is not yet defined precisely. | Phase 5 rules |
| 6 | Greeks and IVs are Deribit's as recorded, unchecked. | A bad Deribit delta picks the wrong leg. | Phase 3 (match Deribit within agreed tolerances) |
| 7 | Decisions once a day (09:00 UTC); v1 checks every minute and allows 2 entries a day. | Entries depend on one minute's quotes. | Phase 5 selector |
| 8 | IV must be above RV with no margin (v1: at least 3 vol points). | More entries than v1 would make. | Phase 5 |
| 9 | No shock filter, no cooldown after a stop, no event filter. | Enters straight after jumps and stops. | Phase 5 |
| 10 | No time exit 24 hours before expiry: a spread is held to settlement unless it hits a take profit or the stop. | Expiry-day gamma; v1 exits earlier. | Phase 5 |
| 11 | The choice among passing spreads is the absolute delta closest to 0.20, not the brief's objective (expected P&L minus Expected Shortfall). | No ranking by value or tail risk. | Phase 5, Phase 7 |
| 12 | Flagged minutes are skipped whole. That covers skip periods the hourly checks already know, plus two live checks: a partial snapshot (under 98% of listed options) and a missing or over-60-second-old index. The "index unchanged for 5 minutes" check is not run live, and the hourly flags arrive an hour late. | Damian's rule (an option flag blocks only spreads using it; a flag on an expiry's future blocks that expiry; global flags block everything) is only partly applied. | Phase 5 (data-quality gating in the candidate generator) |
| 13 | Per-option problems are caught only by the leg checks (two-sided, not crossed, bid-ask, freshness). IV outliers and flags on the expiry's future are not checked. | An option with a bad mark IV can still be traded. | Phase 5 |
| 14 | When the service is down (deploy, reboot), it processes only the latest snapshot on return: missed minutes are not replayed. | A take profit or stop hit during the gap is taken late, at a different price. | Phase 4 event loop; Phase 8 replay check |
| 15 | Fills at the top-of-book bid or ask of the snapshot minute: no latency, slippage or market impact. Entry size is cut to the quoted size. An exit waits until the quoted size covers the whole position. No partial fills. | Optimistic on fill prices, pessimistic on exit timing. | Phase 4 fills (partial fills beyond the quoted size); Phase 8 testnet |
| 16 | Legs fill one at a time, not as a combo, and both legs pay the fee. Deribit's combo discount would make the cheaper direction's fees zero. | Fees are about twice what a combo would pay. | Phase 4 (fee schedule), Phase 8 (combo orders) |
| 17 | Standard fee tier, taker on every leg, no rounding to Deribit's fee precision. The fee base is the index price in the quote row. | Small differences from real fees. | Phase 4 |
| 18 | Exits are judged on executable prices before fees, with no bid-ask width check. | A momentary wide ask can trigger a stop. | Phase 4/5 |
| 19 | Delivery fee: one per in-the-money leg, as the brief says. Deribit's announcement of the new delivery process (https://insights.deribit.com/?p=113365) says a delivery fee is still paid on each expiring in-the-money option, before any netting on the future. Netting only lowers the delivery fee of a futures position, and the future created from an option pays no second fee (see also https://support.deribit.com/hc/en-us/articles/31424932728093-Linear-USDC-Options). v1 holds no futures, so a spread with both legs in the money pays the option delivery fee on each in-the-money leg and does not pay less. | Not a shortcut in the fee: only the booking through the future is simplified (row 22). | Phase 4 settlement (the fee stays) |
| 20 | A single-entry cash ledger (the sum of fills), not double-entry, with no reconciliation test. | Accounting errors would go unnoticed. | Phase 4 double-entry ledger |
| 21 | Equity marks open spreads at Deribit's mark price. A minute where a leg has no mark gets no equity row. | Gaps in the equity series. | Phase 4 |
| 22 | Settlement pays intrinsic value at the delivery price directly, not through the expiry's future. | The same payoff, but it is not the two-step booking. | Phase 4 settlement |
| 23 | Sizing counts the entry fees in the maximum loss, but not exit or delivery fees. Equity for sizing is at mark. | The real maximum loss is a few USDC higher. | Phase 4 risk gateway |
| 24 | Limits checked: open count, per expiry, total maximum loss. There are no portfolio delta or vega limits, no daily loss limit, no kill switch and no `risk_events` log (a refusal shows only in the day's decision reason). | Most of the risk table is not enforced. | Phase 4 risk gateway; Phase 8 kill switch |
| 25 | No margin model: 100,000 USDC paper cash with no margin requirement. | Could hold positions a real account couldn't. | Phase 4/8 |
| 26 | The `rough_*` tables are not in the nightly R2 export. | The paper ledger is lost if the database is lost. | Phase 8 (paper ledger backed up), if kept |
| 27 | No healthchecks.io ping for the paper service. The page shows "last seen" and an error after 5 minutes without a run. | A stopped paper service emails nobody. | Phase 8 |
| 28 | The maths has hand-worked golden tests only, not btc-vol-models numbers or Deribit's values. | Matches the formulas as written, not proven against references. | Phase 3 golden tests |

Not a shortcut: "no new entries within 48 hours of expiry" is covered by the 7-day minimum.

## Phase 3 status (18 Nov 2026)

The tested replacements for shortcuts 1 to 4, 6 and 28 are **built**, and the Phase 3 gate
passed on 18 Nov 2026. The running rough service **keeps its shortcuts**: Damian decided on 18
Nov 2026 (add-on c) not to switch it onto the new code, because that would change its rule while
its own gate is still open. Phase 4 and 5 replace it.

| # | Replacement, built in Phase 3 | Rough service |
| --- | --- | --- |
| 1 | Our own IV solver (`optlab.pricing.iv`), a spline smile per expiry and the ATM, 25- and 10-delta buckets (`optlab.volatility.smile`, `surface`) | not switched |
| 2 | The surface's 30-day tenor, interpolated in total variance from every usable expiry, flat outside (`optlab.volatility.surface`); both books are fitted | not switched |
| 3 | Forwards from the dated future's mark, with put-call parity as fallback and cross-check (`optlab.volatility.forward`) | not switched |
| 4 | Realised vol from our own 08:00 UTC index snapshots, delivery prices filling in until 13 Dec (`optlab.volatility.realised`, `features.rv_30d`) | not switched |
| 6 | Our Black-76 Greeks match Deribit's to its 5-decimal rounding (16 Nov), and on 17 Nov every check is inside the agreed tolerances (`research/deribit_comparison.md`) | not switched (it uses Deribit's delta, which now has a check) |
| 28 | Golden tests against btc-vol-models (`tests/pricing/golden/`), plus finite differences and Deribit's values | done (tests only) |
