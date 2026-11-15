# Liquidity report: BTC_USDC against inverse BTC options (Phase 2)

Generated 2026-11-15 01:03 UTC by `research/liquidity_report.py` in 44 s.

- Data: 2 exported UTC days, 2026-11-13 to 2026-11-14 (dataset version 017271df668bbca8).
- Snapshot minutes: 1,887 recorded, 1,885 usable after the data-quality skips, every 5th sampled: 377 minutes from 2026-11-13 16:33 to 2026-11-14 23:59 UTC.
- Candidate spreads follow the v1 entry rules: 7 to 30 days to expiry; short put delta -0.30 to -0.15 (bull put) or short call delta 0.15 to 0.30 (bear call), Deribit's delta; long leg the nearest further strike 2% to 6% of the forward away.
- Liquidity rule (v1): both legs two-sided, bid-ask at most 10% of mid, quotes under 2 minutes old, top-of-book size at least the order size. Order size: maximum loss (width minus credit) at most 2% of 100,000 USDC. Inverse premiums in USD at the index price; credit before fees.

## Results

| Measure | BTC_USDC (linear) | BTC (inverse) |
| --- | --- | --- |
| Expiries in the 7-30 day window | 2026-11-27 | 2026-11-27, 2026-12-04 |
| Minutes with any candidate spread | 100% (377 of 377) | 100% (377 of 377) |
| **Minutes with a spread passing the liquidity rule** | 100% (376 of 377) | 99% (372 of 377) |
| ... bull put side | 100% (376 of 377) | 94% (356 of 377) |
| ... bear call side | 98% (370 of 377) | 79% (296 of 377) |
| ... and credit at least 20% of width (before fees) | 46% (173 of 377) | 27% (100 of 377) |
| Candidate spreads (spread-minutes) | 1,784 | 3,724 |
| Share of spreads: both legs two-sided | 100% | 100% |
| Share of spreads: both legs bid-ask at most 10% of mid | 89% | 44% |
| Share of spreads: both quotes under 2 minutes old | 100% | 100% |
| Share of spreads: top-of-book size covers the order | 98% | 94% |
| Share of spreads: **all four (liquidity rule)** | 88% | 40% |
| Share of spreads: credit at least 20% of width | 10% | 5% |
| Median bid-ask of short leg (of mid) | 4% | 7% |
| 90th percentile, short leg | 6% | 13% |
| Median bid-ask of long leg (of mid) | 6% | 10% |
| 90th percentile, long leg | 10% | 15% |
| Median short-leg bid size (BTC) | 10.00 | 76.20 |
| Median long-leg ask size (BTC) | 10.00 | 63.05 |
| Median order size allowed by sizing (BTC) | 1.16 | 1.15 |
| Median width (of forward) | 3% | 3% |
| Median credit per BTC (USD, before fees) | 300 | 273 |
| Trades on candidate legs | 181 (690.3 BTC, 24 instruments) | 3,133 (25,511.1 BTC, 64 instruments) |
| All option trades in the book | 1,406 | 18,710 |

## Recommendation

Written by hand after the run above; the script keeps this section when it regenerates the
report. Choosing the book is Damian's call.

**Main book: BTC_USDC (linear), with inverse BTC options as the fallback, as the brief
planned.** It is not too thin for v1.

- **Tighter quotes where v1 trades.** The median bid-ask is 4% of mid on the short leg and 6%
  on the long leg, against 7% and 10% for inverse. 88% of USDC candidate spreads pass the full
  liquidity rule, against 40% for inverse, and nearly all of the inverse failures are the
  "bid-ask at most 10% of mid" test (44% of inverse spreads pass it).
- **Enough size.** Top-of-book size covers the order the sizing rule allows (median 1.16 BTC)
  on 98% of USDC spreads. Inverse shows much more size (median 76 BTC at the short leg's bid),
  but v1 needs only about 1 BTC.
- **No entry minutes lost.** Both books had a spread passing the rule in almost every sampled
  minute (376 and 372 of 377), so choosing USDC gives up no entry opportunities in this data.
- **One currency.** USDC premiums, profit and loss and the 100,000 USDC account share a
  currency, so sizing, margin and the ledger need no BTC conversion.

Caveats:

- **USDC trades much less.** 1,406 option trades in the book over the two days, against 18,710
  for inverse; 181 trades on USDC candidate legs. The quotes are tight and deep, but few trades
  print, fills beyond the top of the book are untested, and testnet fills will differ from the
  live book.
- **Fewer USDC expiries.** On 14 Nov BTC_USDC listed 15, 16, 17, 18, 20 and 27 Nov and 25 Dec;
  inverse also listed 4 Dec. So the 7-30 day window held only 27 Nov for USDC (27 Nov and
  4 Dec for inverse). Unless Deribit lists another USDC expiry, none is in the window from
  20 Nov 08:00 to 25 Nov 08:00 UTC, and v1 would find no USDC entry in those five days. Open
  question for Damian: no trade then, or fall back to inverse?
- **The minimum credit binds, not liquidity.** "Credit at least 20% of width" (before fees)
  holds for 10% of USDC spreads (5% inverse), in 46% of sampled minutes (27% inverse).
- **Little data.** About 31 hours, from Friday 13 Nov 16:33 to Saturday 14 Nov 23:59 UTC,
  most of it a weekend day. Re-run `research/liquidity_report.py` on a few weeks of data before
  Phase 5 runs the strategies; the inverse book keeps being recorded either way.
