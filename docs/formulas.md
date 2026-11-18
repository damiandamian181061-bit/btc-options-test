# Formula sheet

The definitions every report and backtest uses (Phase 5 builds them; Phase 3 builds realised
vol). **Signed off by Damian on 18 Nov 2026** (Phase 3 add-on k): the choices marked
**(choice n)** are listed at the end with his answers (he took each recommendation).

Everything is annualised over **365 days**: BTC trades every day of the year.

## 1. Returns

Daily equity `E_d` is the account's equity at the daily cut, **00:00 UTC** (choice 1), the same
UTC day the brief's daily loss limit uses.

- Simple daily return: `r_d = E_d / E_{d-1} - 1`. Used for P&L-based metrics, because simple
  returns add up across positions.
- Log return: `l_d = ln(E_d / E_{d-1})`. Used for volatility of prices (section 2).
- Total return over n days: `E_n / E_0 - 1`.
- Annualised return (CAGR): `(E_n / E_0)^(365 / n) - 1`.

## 2. Realised volatility of BTC

Close to close, on the index (`btc_usdc` for the main book) at **08:00 UTC** each day (choice 4):

    l_d = ln(C_d / C_{d-1});   RV_n = sqrt(365 x (1/n) x sum of l_d^2 over the last n days)

- **Zero mean**: the mean daily log return isn't subtracted. Over a month it is mostly noise,
  and subtracting it would understate volatility after a steady trend.
- The n + 1 closes must be on consecutive days, or there is no value.
- Until our own recording covers the window (13 Dec 2026 for 30 days), missing closes are
  Deribit's 08:00 delivery prices, each a 30-minute average of the index, so slightly smoother.
  The features table records how many were used (`rv_30d_delivery`).
- Windows: 7 and 30 days. The rough version's RV30 was the same formula on delivery prices only.

## 3. Risk-adjusted return

On the daily simple returns, with a risk-free rate of **0** (choice 2: paper capital in USDC
earns nothing):

- Volatility: `sigma_d = sample standard deviation of r_d` (n - 1 in the denominator);
  annualised `sigma_d x sqrt(365)`.
- **Sharpe**: `mean(r_d) / sigma_d x sqrt(365)`.
- **Sortino**: `mean(r_d) / DD x sqrt(365)`, with downside deviation
  `DD = sqrt((1/n) x sum of min(r_d, 0)^2)` over **all** n days (target 0).
- **Maximum drawdown**: `MDD = max over d of (peak_d - E_d) / peak_d`, where `peak_d` is the
  highest equity up to day d.
- **Calmar**: `CAGR / MDD`.

## 4. Tail risk

Historical simulation on the daily simple returns (choice 3). Sort the n returns from worst to
best, `r_(1) <= r_(2) <= ...`, and let `k = max(1, ceiling(n x (1 - alpha)))`:

- **Value at Risk**: `VaR_alpha = -r_(k)`, the loss not exceeded on a fraction alpha of days.
- **Expected Shortfall**: `ES_alpha = -mean(r_(1), ..., r_(k))`, the average loss on the worst
  k days. Always at least VaR.
- Levels: 95% and 99% in reports; 97.5% in the ranking objective (the Basel level).
- Losses are reported as positive percentages of equity. **Worst day** is `-r_(1)`.
- With fewer than 100 days, 99% ES is just the worst day, and with fewer than 20 days so is 95%
  ES: reports say how many days stand behind each figure.

## 5. Thesis hit rate

`thesis met / trades closed`, counting every closed or settled trade once.

## Worked example

Made-up equity at 00:00 UTC over 10 days (USDC): 100,000; 100,400; 100,150; 99,600; 99,900;
100,700; 100,500; 98,900; 99,400; 100,100; 100,600.

| Day | Equity | Simple return | Log return |
| --- | --- | --- | --- |
| 1 | 100,400 | +0.4000% | +0.3992% |
| 2 | 100,150 | -0.2490% | -0.2493% |
| 3 | 99,600 | -0.5492% | -0.5507% |
| 4 | 99,900 | +0.3012% | +0.3008% |
| 5 | 100,700 | +0.8008% | +0.7976% |
| 6 | 100,500 | -0.1986% | -0.1988% |
| 7 | 98,900 | -1.5920% | -1.6048% |
| 8 | 99,400 | +0.5056% | +0.5043% |
| 9 | 100,100 | +0.7042% | +0.7018% |
| 10 | 100,600 | +0.4995% | +0.4983% |

1. Mean daily return: 0.06225%. Sample standard deviation: 0.72968%, annualised
   0.72968% x sqrt(365) = 13.941%.
2. **Sharpe** = 0.06225 / 0.72968 x 19.105 = **1.630**.
3. Downside deviation: the four losing days squared, divided by all 10 days:
   sqrt((0.2490^2 + 0.5492^2 + 0.1986^2 + 1.5920^2) / 10) = 0.54200%.
   **Sortino** = 0.06225 / 0.54200 x 19.105 = **2.194**.
4. Total return +0.6000%; **CAGR** = 1.006^(365/10) - 1 = **24.40%** (ten days scaled to a year:
   meaningless over so short a period, which is why reports state the period).
5. **Maximum drawdown**: peak 100,700 (day 5) to trough 98,900 (day 7): 1,800 / 100,700 =
   **1.7875%**. **Calmar** = 24.40 / 1.7875 = **13.65**.
6. **Tail risk**: n = 10, so k = ceiling(10 x 0.05) = 1 at 95%, and 1 at 97.5% and 99% too.
   VaR and ES at every level are the worst day, **1.592%**. With 100 days, 95% ES would be the
   mean of the 5 worst days.
7. **Worst day**: 1.592% (day 7).

## Damian's choices (decided 18 Nov 2026: each recommendation accepted)

Decided: (1) daily cut 00:00 UTC; (2) risk-free rate 0; (3) VaR and ES by historical simulation
on daily returns; (4) realised vol on 08:00 UTC index closes. The options as they were put:

1. **Daily cut for equity returns.** Recommended: **00:00 UTC**, the UTC day the daily loss limit
   already uses. Alternative: 08:00 UTC, the expiry time (P&L days then line up with
   settlements).
2. **Risk-free rate in Sharpe and Sortino.** Recommended: **0**: the paper USDC earns nothing, so
   any return is excess return. Alternative: a USDC lending rate, which would lower every Sharpe
   by about that rate divided by the volatility.
3. **VaR and ES method.** Recommended: **historical simulation on daily returns**, as above:
   no distribution assumed, and BTC's fat tails are in the data. Alternatives: a normal
   distribution (understates tails), or EVT tails (Phase 7's tail model, later).
4. **Realised-vol close.** Recommended: **the index at 08:00 UTC**, matching the expiries and the
   delivery prices that fill in before 13 Dec. Alternative: 00:00 UTC closes.
