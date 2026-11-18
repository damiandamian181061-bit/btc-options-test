# Conventions: units, time, prices and Greeks

Every module follows these. They extend the conventions in `CLAUDE.md` and were checked against
Deribit's own numbers on 16 Nov 2026 (`research/deribit_comparison.md`).

## Time

- Every timestamp is an integer count of **UTC milliseconds**. A snapshot minute is the minute
  the recorder stamped the row (`ts`); Deribit's own ticker time is `exchange_ts_ms` (about 1 to
  2 seconds earlier).
- **Time to expiry** tau is in years, ACT/365: `tau = (expiry_ms - t_ms) / (365 x 86,400,000)`.
  Every Deribit BTC expiry is at **08:00 UTC**. Our pricing uses the snapshot minute as t;
  Deribit computes its marks and Greeks at its ticker time, which is why a comparison uses
  `exchange_ts_ms`.
- Days are UTC days. "Daily" data (closes, delivery prices) is taken at 08:00 UTC, the moment
  expiries settle, unless a formula says otherwise (`docs/formulas.md`).

## Instruments, prices and currencies

| Instrument | Example | Price currency | Size | Tick |
| --- | --- | --- | --- | --- |
| Linear option (main book) | `BTC_USDC-27NOV26-60000-P` | USDC per 1 BTC | BTC | 5 USDC |
| Inverse option (fallback book) | `BTC-27NOV26-60000-P` | BTC per 1 BTC | BTC | 0.0001 BTC |
| Linear dated future | `BTC_USDC-27NOV26` | USDC | BTC | 2.5 USDC |
| Inverse dated future | `BTC-27NOV26` | USD | USD contracts | 2.5 USD |
| Perpetuals | `BTC_USDC-PERPETUAL`, `BTC-PERPETUAL` | USDC, USD | | |
| Indexes | `btc_usdc`, `btc_usd` | USDC, USD | | |

- Every money value carries its currency. Cash ledgers use exact decimals; pricing maths uses
  floating point.
- An empty side of the book is `NULL` with size 0, never a zero price.
- Deribit's rounding (seen in the recorded data): mark IV to 0.01 vol point; BTC_USDC marks to
  0.01 USDC; BTC marks to 0.0001 BTC; delta, gamma, vega and theta to 5 decimals.

## Implied volatility and moneyness

- IV is a **decimal**: 0.45 means 45%. Deribit sends percent; the recorder stores decimals.
- A **vol point** is 0.01 of IV (45% to 46%).
- **Log-moneyness** `k = ln(K / F)`, with F the forward of the option's own expiry.
- **ATM** means at the forward (k = 0), not at the index.

## The pricing model: Black-76 on the forward, undiscounted

Deribit prices BTC options with Black-76 on each expiry's forward and an interest rate of 0
(`interest_rate` is 0 on every option of both books). So there is no discounting:

    d1 = (ln(F/K) + sigma^2 tau / 2) / (sigma sqrt(tau)),   d2 = d1 - sigma sqrt(tau)
    call = F N(d1) - K N(d2),   put = K N(-d2) - F N(-d1)
    put-call parity: call - put = F - K

`optlab.pricing.black76` implements it; `optlab.pricing.iv` inverts it (Newton, Brent fallback).

### The forward

For each expiry, the forward is the **mark price of the dated future of the same expiry** (Deribit
lists one for every BTC_USDC option expiry, and the recorder stores it every minute). Where there
is none, or it has no mark, the forward comes from **put-call parity** on the three strikes
nearest it (median, two-sided quotes only). Parity is also the cross-check: on 16 Nov the two
agreed to a median 1.5 basis points. Deribit's own `underlying_price` is its forward for that
option; ours agreed with it to a median 0.5 bp (linear) and 0.1 bp (inverse).

## Greeks, in Deribit's units

| Greek | Formula | Units | Per |
| --- | --- | --- | --- |
| delta | call N(d1), put N(d1) - 1 | BTC per contract | 1 USD of the forward |
| gamma | n(d1) / (F sigma sqrt(tau)) | delta per USD | 1 USD of the forward |
| vega | F n(d1) sqrt(tau) / 100 | USD | 1 vol point |
| theta | -F n(d1) sigma / (2 sqrt(tau)) / 365 | USD | 1 calendar day |

- These are the units of Deribit's ticker for **both books**: vega and theta are in USD (USDC)
  even for inverse options, and delta is **not premium-adjusted**. On 16 Nov our Greeks at
  Deribit's mark IV, forward and ticker time matched Deribit's to its 5-decimal rounding.
- **Near expiry Deribit differs**, by design, and we don't copy it: within about 4 days its theta
  is no longer the instantaneous Black-76 theta (in the last day it is close to the one-day change
  in value), and in the last 30 minutes its delta reflects the settlement average (07:30 to 08:00
  UTC). The strategy enters at 7 to 30 days, so neither affects leg choice.
- For an inverse option held in BTC, the BTC-denominated (premium-adjusted) delta is
  `N(d1) - V_btc` for a call and `N(d1) - 1 - V_btc` for a put, where `V_btc` is the BTC premium.
  It matters only for hedging a BTC account, which v1 doesn't do; it is not what Deribit reports.

## Inverse options in USDC

An inverse option's premium is in BTC. Two different conversions apply, and they are kept
apart:

1. **Valuation** (pricing, IVs, comparing books): Deribit's BTC premium is the Black-76 USD price
   divided by the expiry's forward, `V_btc = V_usd / F`. So `V_usd = V_btc x F` with the **same
   forward**. On 16 Nov this matched Deribit's marks to their 0.0001 BTC rounding; dividing by
   the index instead was off by up to 0.1 BTC.
2. **Cash** (the ledger): BTC paid or received stays a **BTC balance**. Its USDC value for equity
   and reports is that balance times the `btc_usdc` index at the same minute, and it moves with
   the index. An inverse option settles in BTC: intrinsic value in USD divided by the delivery
   price.

The main book is BTC_USDC (linear), so v1 trades only USDC premiums; the inverse rules exist for
the fallback book and for comparisons.

## The surface

- **Smile** per expiry: a smoothing spline of IV in k through the out-of-the-money quotes' mid
  IVs (puts below the forward, calls at or above it), weighted by 1 / (ask IV - bid IV). Flat
  outside the quoted strikes.
- **Buckets**: ATM (k = 0), 25-delta and 10-delta calls and puts, by forward delta on the fitted
  smile (not premium-adjusted).
- **Tenors** 1, 3, 7, 14, 30, 60 and 90 days: total variance `iv^2 tau` interpolated linearly in
  tau between the two expiries around the tenor, at the same bucket; flat IV outside the listed
  expiries (labelled "extrapolated"). Expiries under 12 hours away are fitted but not used.
- **Checks** stored with every fit: butterfly (Gatheral's density condition on total variance)
  and calendar (total variance must not fall with time at the same k).

## Realised volatility

Close to close on the index at 08:00 UTC, zero mean, annualised over 365 days
(`docs/formulas.md`). Before our own recording covers the window, Deribit's 08:00 delivery
price stands in for the missing closes, and each result says how many it used.
