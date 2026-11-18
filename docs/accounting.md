# Accounting rules

How the Phase 4 ledger books USDC cash, option premiums, fees, settlement and funding.
**Signed off by Damian on 18 Nov 2026** (Phase 3 add-on k); his choices 5 and 6 are at the end
(1 to 4 are in `docs/formulas.md`). Fee rates are Damian's confirmed schedule of 15 Nov 2026
(`configs/fees/deribit.yaml`, Standard tier).

## Principles

1. **Double entry, exact decimals.** Every event is a journal entry whose debits equal its
   credits, in one currency. Amounts are Python `Decimal`s stored as PostgreSQL `numeric`; nothing
   is rounded unless Deribit rounds it.
2. **One cash account per currency.** The main book (BTC_USDC) uses only `cash:USDC`. The
   inverse book would add `cash:BTC`; BTC is never silently converted (`docs/conventions.md`).
3. **Fills at executable prices.** A sell fills at the bid, a buy at the ask, within quoted size.
   Marks are used only to value open positions, never to fill.
4. **Fees are expenses, never netted into prices.** Trading and delivery fees each have their
   own account, so cost sensitivity (0.5 to 3 times) can scale them.
5. **Realised when closed.** P&L is realised when a position is bought back or settles; until
   then it is unrealised, from the marks.
6. **Reconciliation.** At every step: change in equity = option P&L + hedge P&L - fees + funding.
   A test checks it to the last decimal.

## Accounts

| Account | Holds | Normal side |
| --- | --- | --- |
| `capital` | paper capital (100,000 USDC) | credit |
| `cash:USDC` | cash | debit |
| `position:<instrument>` | each open position at its entry value (premium paid, or premium received as a liability) | debit if long, credit if short |
| `settlement:<future>` | the future an in-the-money option becomes at expiry, for an instant | either |
| `pnl:options` | realised option P&L | credit if a gain |
| `pnl:hedge` | realised perpetual P&L (hedged benchmarks only) | credit if a gain |
| `fees:trading`, `fees:delivery` | fees paid | debit |
| `funding` | funding paid or received on perpetuals | either |

Equity = cash + the open positions **at mark** (choice 5): a long position is worth
`size x mark`, a short one `-size x mark`. Unrealised P&L = that value minus the positions'
entry values.

## Booking rules

**Opening or adding to a position.** Premium = price x size, in the option's currency.

- Sell (short): Dr `cash`, Cr `position` (we owe the option's value).
- Buy (long): Dr `position`, Cr `cash`.

**Trading fee**, on every leg, taker (paper fills cross the spread):
`fee = min(0.03% x index x size, 12.5% x price x size)`, with the book's index at the fill.
Dr `fees:trading`, Cr `cash`. Spreads fill leg by leg, and both legs pay (Deribit's combo
discount is not modelled unless Damian decides otherwise).

**Closing (buying back or selling out).** The closing fill at its executable price, its own
trading fee, and the difference between the position's entry value and the closing premium to
`pnl:options`.

**Settlement at expiry, 08:00 UTC, at Deribit's delivery price P** (the two-step process for
BTC_USDC options since April 2026):

1. **Out of the money** (call with P <= K, put with P >= K): expires worthless, no fee. The
   entry value goes to `pnl:options`.
2. **In the money**, step 1: the option becomes a position in its expiry's future at the strike
   (a long call or a short put becomes long the future; a short call or a long put becomes
   short it). The option position closes at its intrinsic value `|P - K| x size`, and the
   difference from its entry value goes to `pnl:options`; the future is carried at that
   intrinsic value in `settlement:<future>`.
3. Step 2: the future cash-settles at P straight away: its value moves to `cash`, and
   `settlement:<future>` is back to zero. Same payoff as paying intrinsic value directly.
4. **Delivery fee**, once per in-the-money option leg, before any netting:
   `min(0.015% x P x size, 12.5% x |P - K| x size)`. Dr `fees:delivery`, Cr `cash`. The future
   created from the option pays no second fee. Daily (non-Friday) expiries pay none, though v1
   never holds them (7 to 30 days at entry means Friday expiries).

**Perpetual hedges and funding** (Set B benchmarks only; v1 doesn't hedge). Perpetual trades are
booked like options (positions at entry value, fees, realised P&L to `pnl:hedge`). Funding paid
or received is Dr or Cr `funding` against `cash`; with a positive funding rate, longs pay
shorts: `amount = size x index x funding rate for the period`. Checked in Phase 4 (18 Nov 2026)
against Deribit's "Funding Specifications": the rate is the perpetual's premium over the index
beyond a +/-0.025% damper, capped (5% for the BTC_USDC perpetual), per 8 hours; Deribit accrues
it continuously and moves it into cash at its 08:00 daily settlement. The backtest samples it
once a minute and books it to cash at once (`docs/backtest.md`; equity is the same).

## Worked example: a bull put spread on BTC_USDC, held to expiry

Made-up quotes. 27 Nov expiry. Index 65,000 USDC at entry. Size 0.5 BTC.

**Entry.** Sell the 60,000 put at its bid, 800; buy the 58,000 put at its ask, 450.

| Entry | Debit | Credit | USDC |
| --- | --- | --- | --- |
| Sell 0.5 60,000 P at 800 | `cash` | `position:60000P` | 400.00 |
| Its fee: min(0.03% x 65,000 x 0.5 = 9.75, 12.5% x 400 = 50) | `fees:trading` | `cash` | 9.75 |
| Buy 0.5 58,000 P at 450 | `position:58000P` | `cash` | 225.00 |
| Its fee: min(9.75, 12.5% x 225 = 28.125) | `fees:trading` | `cash` | 9.75 |

Cash: +400 - 9.75 - 225 - 9.75 = **+155.50**. Credit 350 a BTC, 175 for 0.5. Maximum loss:
width 2,000 x 0.5 - 175 + 19.50 fees = **844.50**, before delivery fees.

**A day later, marked.** Marks 950 (60,000 P) and 520 (58,000 P). Open value
-0.5 x 950 + 0.5 x 520 = **-215.00**; at entry it was -400 + 225 = -175, so unrealised P&L is
**-40.00**. Equity = 100,000 + 155.50 - 215.00 = **99,940.50** (= 100,000 - 19.50 fees - 40.00
unrealised).

**Settlement, three outcomes.**

| Delivery price | 60,000 P | 58,000 P | Delivery fees | P&L of the spread |
| --- | --- | --- | --- | --- |
| 66,000 | worthless | worthless | 0 | 175 - 19.50 = **+155.50** |
| 59,000 | -1,000 x 0.5 = -500 | worthless | min(0.015% x 59,000 x 0.5 = 4.425, 12.5% x 500) = 4.425 | 175 - 19.50 - 500 - 4.425 = **-348.925** |
| 57,000 | -3,000 x 0.5 = -1,500 | +1,000 x 0.5 = +500 | 4.275 + 4.275 = 8.55 | 175 - 19.50 - 1,500 + 500 - 8.55 = **-853.05** |

At 57,000 both legs are in the money: the loss is the maximum loss, 844.50, plus the two
delivery fees, 8.55.

The 59,000 case, journal by journal:

| Entry | Debit | Credit | USDC |
| --- | --- | --- | --- |
| 60,000 P closes at intrinsic 500: entry value 400, the extra 100 is a loss | `position:60000P` 400, `pnl:options` 100 | `settlement:BTC_USDC-27NOV26` | 500.00 |
| The future (long 0.5 at 60,000) settles at 59,000 | `settlement:BTC_USDC-27NOV26` | `cash` | 500.00 |
| Delivery fee on the 60,000 P | `fees:delivery` | `cash` | 4.425 |
| 58,000 P expires worthless | `pnl:options` | `position:58000P` | 225.00 |

Option P&L: -100 - 225 = **-325.00** (= the credit 175 minus the 500 paid). Fees: 19.50 trading
and 4.425 delivery. Total **-348.925**, exactly the cash change: +155.50 - 500 - 4.425.

**Funding (illustration only).** A short perpetual hedge of 0.2 BTC with a funding rate of
+0.01% for the period and the index at 65,000 receives 0.2 x 65,000 x 0.0001 = **1.30 USDC**:
Dr `cash`, Cr `funding`.

**Inverse book (illustration only).** Selling 0.5 of an inverse put at 0.0123 BTC books
**0.00615 BTC** in `cash:BTC` (Dr `cash:BTC`, Cr `position`), never converted. At an index of
65,000 it is worth 399.75 USDC in equity, and that value moves with the index.

## Damian's choices (decided 18 Nov 2026: each recommendation accepted)

Decided: (5) open positions valued at Deribit's mark price, with the liquidation value shown
alongside on reports; (6) BTC amounts reported in USDC at the same minute's `btc_usdc` index,
BTC balances kept in BTC. The options as they were put:

5. **Valuing open positions for equity.** Recommended: **Deribit's mark price**, the exchange's
   own fair value (what its margin and P&L use), with the **liquidation value** (bids for longs,
   asks for shorts) shown alongside on reports. Alternative: liquidation value only. It is more
   conservative, but a wide momentary ask makes equity jump, and drawdown with it.
6. **BTC amounts in USDC.** Recommended: **the `btc_usdc` index of the same minute** for reporting,
   with BTC balances kept in BTC. Alternative: convert BTC to USDC at each fill, which would hide
   the BTC exposure an inverse book really carries.
