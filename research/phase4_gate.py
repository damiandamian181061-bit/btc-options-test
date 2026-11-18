"""Phase 4 gate on a real recorded week: determinism, leakage, reconciliation, smoke trade.

    uv run python research/phase4_gate.py [--data-dir data] [--out research/phase4_gate.md]

Needs the week's days synced from R2 (``optlab-data sync --from 2026-11-14 --to 2026-11-20``).
Writes a Markdown report with the checks and the smoke trade step by step; no raw data.

1. Determinism: the exercise config (random spreads and perpetual hedges) runs twice; the two
   result hashes must be equal.
2. Leakage: a copy of the week where every quote, index and delivery price after a cut-off is
   changed (prices x 1.05, every 7th minute dropped) runs again; every order, fill, position,
   equity row and risk event up to the cut-off must be identical, and the result hash differ.
3. Reconciliation: the engine checks at every minute that the change in equity equals option
   P&L + hedge P&L - fees + funding, exactly, and that the journal balances; the report
   re-checks it from the equity rows and counts the minutes and the P&L parts involved.
4. The smoke trade (configs/backtests/smoke.yaml): every number, for checking by hand.
"""

import argparse
import itertools
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from optlab.domain.clock import MS_PER_MINUTE, ms_to_datetime
from optlab.market_data.view import ParquetSource
from optlab.simulator.config import BacktestConfig, load_backtest_config
from optlab.simulator.engine import Backtest, BacktestResult
from optlab.simulator.runinfo import dataset_hash, git_state

CUT = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
PRICE_COLUMNS = ("bid", "ask", "mark_price", "index_price", "underlying_price",
                 "estimated_delivery_price", "last_price")  # fmt: skip


def fmt_ts(ts_ms: int) -> str:
    return f"{ms_to_datetime(ts_ms):%a %d %b %H:%M} UTC"


def run(cfg: BacktestConfig, data: Path) -> tuple[BacktestResult, float]:
    t0 = time.monotonic()
    result = Backtest(cfg, ParquetSource.from_data_dir(data)).run()
    return result, time.monotonic() - t0


# ------------------------------------------------------------------------------- leakage copy


def _scale(table: pa.Table, mask: pa.Array) -> pa.Table:
    for name in PRICE_COLUMNS:
        if name in table.column_names:
            col = table.column(name)
            scaled = pc.multiply(col, pa.scalar(1.05, col.type))
            i = table.schema.get_field_index(name)
            table = table.set_column(i, table.schema.field(i), pc.if_else(mask, scaled, col))
    return table


def scrambled_copy(data: Path, dest: Path, cut: datetime) -> None:
    """Copy the exports and change everything after ``cut``."""
    src = data / "exports" / "v1"
    shutil.copytree(src, dest / "exports" / "v1")
    root = dest / "exports" / "v1"
    cut_ms = int(cut.timestamp() * 1000)
    for table in ("option_quotes", "underlying_ticks"):
        for f in sorted((root / table).rglob("*.parquet")):
            t = pq.read_table(f)
            ts_ms = pc.cast(pc.cast(t.column("ts"), pa.timestamp("ms", tz="UTC")), pa.int64())
            after = pc.greater(ts_ms, cut_ms)
            if not pc.any(after).as_py():
                continue
            minute = pc.divide(ts_ms, MS_PER_MINUTE)
            dropped = pc.and_(after, pc.equal(pc.bit_wise_and(minute, 7), 0))  # every 8th
            t = _scale(t, after).filter(pc.invert(dropped))
            pq.write_table(t, f, compression="zstd")
    for f in sorted((root / "delivery_prices").rglob("*.parquet")):
        t = pq.read_table(f)
        after = pc.greater(t.column("recorded_ms"), cut_ms)
        prices = [
            str(Decimal(p) * Decimal("1.05")) if a else p
            for p, a in zip(t.column("delivery_price").to_pylist(), after.to_pylist(), strict=True)
        ]
        i = t.schema.get_field_index("delivery_price")
        t = t.set_column(i, t.schema.field(i), pa.array(prices, pa.string()))
        pq.write_table(t, f, compression="zstd")


def upto(result: BacktestResult, cut_ms: int) -> dict[str, list[Any]]:
    return {
        "orders": [r for r in result.orders if r.ts_ms <= cut_ms],
        "fills": [r for r in result.fills if r.ts_ms <= cut_ms],
        "positions": [r for r in result.positions if r.ts_ms <= cut_ms],
        "equity": [r for r in result.equity if r.ts_ms <= cut_ms],
        "risk_events": [r for r in result.risk_events if r.ts_ms <= cut_ms],
    }


# ------------------------------------------------------------------------------- report


def strike_of(source: ParquetSource, name: str, as_of_ms: int) -> Decimal:
    """From Deribit's instrument metadata as recorded (never parsed from the name)."""
    table = source.instruments(as_of_ms)
    row = table.filter(pc.equal(table.column("instrument_name"), name)).to_pylist()
    return Decimal(repr(row[0]["strike"]))


def smoke_report(result: BacktestResult, source: ParquetSource) -> list[str]:
    out: list[str] = []
    buys = [f for f in result.fills if f.kind == "trade"]
    setts = [f for f in result.fills if f.kind == "settlement"]
    if not buys:
        return ["**No smoke trade filled.** Orders: "
                + "; ".join(f"{o.status} {o.detail}" for o in result.orders)]  # fmt: skip
    buy = buys[0]
    (group,) = result.groups
    premium = buy.price * buy.amount
    index = buy.index_price or Decimal(0)
    fee_index = Decimal("0.0003") * index * buy.amount
    fee_cap = Decimal("0.125") * buy.price * buy.amount
    out += [
        f"- Thesis: {group.thesis}.",
        f"- **Entry** {fmt_ts(buy.ts_ms)}: buy {buy.amount} BTC of `{buy.instrument}` at the ask "
        f"**{buy.price}** USDC (mark {buy.mark}, mid {buy.mid}); btc_usdc index **{index}**.",
        f"- Premium = {buy.price} x {buy.amount} = **{premium}** USDC.",
        f"- Trading fee = min(0.03% x {index} x {buy.amount} = {fee_index}, 12.5% x {buy.price}"
        f" x {buy.amount} = {fee_cap}) = **{buy.fee}** USDC "
        f"({'the index part' if buy.fee == fee_index else 'the cap'} applies).",
        f"- Cash after entry = 100,000 - {premium} - {buy.fee} = "
        f"**{Decimal(100000) - premium - buy.fee}** USDC.",
        "",
        "Mark each day at 00:00 UTC (and the last minute before expiry):",
        "",
        f"| Time | Option mark | Value of {buy.amount} BTC at mark | Equity | Equity at the bid |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows = [r for r in result.equity if r.ts_ms % 86_400_000 == 0]
    expiring = [r for r in result.equity if r.open_groups == 1]
    if expiring:
        rows.append(expiring[-1])
    for r in rows:
        mark = r.option_value / buy.amount if buy.amount else Decimal(0)
        out.append(f"| {fmt_ts(r.ts_ms)} | {mark} | {r.option_value} | {r.equity} | "
                   f"{r.liquidation_equity} |")  # fmt: skip
    if setts:
        s = setts[0]
        dp = s.delivery_price or Decimal(0)
        strike = strike_of(source, buy.instrument, buy.ts_ms)
        fee_rate = Decimal("0.00015") * dp * s.amount
        fee_cap = Decimal("0.125") * s.price * s.amount
        final = result.final
        if final is None:
            return [*out, "\n**No equity rows.**"]
        out += [
            "",
            f"- **Settlement** {fmt_ts(s.ts_ms)}: Deribit's delivery price (07:30-08:00 average "
            f"of the index) **{dp}**.",
            f"- Intrinsic value = max({dp} - {strike}, 0) = **{s.price}** a BTC; for "
            f"{s.amount} BTC: **{s.price * s.amount}** USDC, booked in two steps: the call "
            "became a long position in the expiry's future at the strike, which settled at the "
            "delivery price.",
            f"- Delivery fee = min(0.015% x {dp} x {s.amount} = {fee_rate}, 12.5% x {s.price} x "
            f"{s.amount} = {fee_cap}) = **{s.fee}** USDC"
            + (" (none: out of the money)" if s.price == 0 else "")
            + ".",
            f"- **Final cash** = 100,000 - {premium} - {buy.fee} + {s.price * s.amount} - {s.fee}"
            f" = **{final.cash}** USDC; P&L **{final.equity - result.capital}** USDC "
            f"(option {final.option_pnl}, fees {final.fees}).",
            "",
            "Journal:",
            "",
            "| # | Time | Kind | Postings (debit +, credit -) |",
            "| --- | --- | --- | --- |",
        ]
        for e in result.journal.entries:
            lines = ", ".join(f"`{a}` {v}" for a, v in e.postings)
            out.append(f"| {e.seq} | {fmt_ts(e.ts_ms)} | {e.kind} | {lines} |")
    else:
        out.append("\n**Not settled within the run.**")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--configs", type=Path, default=Path("configs/backtests"))
    p.add_argument("--out", type=Path, default=Path("research/phase4_gate.md"))
    args = p.parse_args()
    data: Path = args.data_dir
    exercise = load_backtest_config(args.configs / "exercise.yaml")
    smoke = load_backtest_config(args.configs / "smoke.yaml")
    source = ParquetSource.from_data_dir(data)
    first = ms_to_datetime(exercise.start_ms).date()
    last = ms_to_datetime(exercise.end_ms - 1).date()
    versions = source.dataset_versions(first, last)
    expected = (last - first).days + 1
    if len(versions) != expected:
        print(
            f"only {len(versions)} of {expected} days synced: {sorted(versions)}", file=sys.stderr
        )
        return 2
    commit, dirty = git_state()
    report = [
        "# Phase 4 gate: the backtest engine on a real recorded week",
        "",
        f"Run {datetime.now(UTC):%d %b %Y %H:%M} UTC at commit `{commit[:7]}`"
        + (" (with uncommitted changes)" if dirty else "")
        + f". Week {first} to {last} (UTC), {len(versions)} exported days, dataset hash "
        f"`{dataset_hash(versions)}`.",
        "",
    ]
    ok = True

    a, ta = run(exercise, data)
    b, tb = run(exercise, data)
    same = a.result_hash == b.result_hash
    ok &= same
    trades = sum(1 for f in a.fills if f.kind == "trade")
    report += [
        "## 1. Determinism",
        "",
        f"`configs/backtests/exercise.yaml` (seed {exercise.seed}) twice: {a.minutes} minutes, "
        f"{len(a.groups)} groups, {trades} trade fills, "
        f"{sum(1 for f in a.fills if f.kind == 'settlement')} settlements, "
        f"{len({o.ticket_id for o in a.orders if o.status == 'rejected'})} rejected tickets "
        f"({ta:.0f} s and {tb:.0f} s).",
        "",
        f"- result hash, run 1: `{a.result_hash}`",
        f"- result hash, run 2: `{b.result_hash}`",
        f"- **{'PASS' if same else 'FAIL'}**",
        "",
    ]

    cut_ms = int(CUT.timestamp() * 1000)
    with tempfile.TemporaryDirectory() as tmp:
        scrambled_copy(data, Path(tmp), CUT)
        c, _ = run(exercise, Path(tmp))
    before_same = upto(a, cut_ms) == upto(c, cut_ms)
    after_differs = a.result_hash != c.result_hash
    ok &= before_same and after_differs
    n = upto(a, cut_ms)
    report += [
        "## 2. Leakage",
        "",
        f"A copy of the week with every quote, index and delivery price after "
        f"{CUT:%d %b %H:%M} UTC multiplied by 1.05 and every 8th minute after it removed.",
        "",
        f"- up to the cut: {len(n['orders'])} order legs, {len(n['fills'])} fills, "
        f"{len(n['equity'])} equity minutes, {len(n['risk_events'])} risk events: "
        f"**{'identical' if before_same else 'DIFFERENT'}**",
        f"- whole run: result hash `{c.result_hash}`, "
        f"**{'different' if after_differs else 'THE SAME'}** (the later data mattered)",
        f"- **{'PASS' if before_same and after_differs else 'FAIL'}**",
        "",
    ]

    rows = a.equity
    breaks = sum(
        1
        for prev, now in itertools.pairwise(rows)
        if now.equity - prev.equity
        != (now.option_pnl - prev.option_pnl) + (now.hedge_pnl - prev.hedge_pnl)
        - (now.fees - prev.fees) + (now.funding - prev.funding)
    )  # fmt: skip
    balanced = all(v == 0 for v in a.journal.trial_balance().values())
    ok &= breaks == 0 and balanced
    final = a.final
    if final is None:
        print("the exercise run recorded no minutes", file=sys.stderr)
        return 2
    report += [
        "## 3. Reconciliation",
        "",
        f"Checked by the engine at every one of the {a.minutes} minutes (it stops on any "
        f"difference), and again here from the equity rows: {breaks} minutes where the change in "
        f"equity differs from option P&L + hedge P&L - fees + funding; journal "
        f"{'balances' if balanced else 'DOES NOT BALANCE'} ({len(a.journal.entries)} entries).",
        "",
        "| Part (whole week, exercise run) | USDC |",
        "| --- | --- |",
        f"| Option P&L | {final.option_pnl} |",
        f"| Hedge P&L | {final.hedge_pnl} |",
        f"| Fees | {final.fees} |",
        f"| Funding | {final.funding} |",
        f"| Change in equity | {final.equity - a.capital} |",
        "",
        f"- **{'PASS' if breaks == 0 and balanced else 'FAIL'}**",
        "",
    ]

    s, _ = run(smoke, data)
    report += ["## 4. Smoke trade (for checking by hand)", "", *smoke_report(s, source), ""]
    report += [f"Overall: **{'PASS' if ok else 'FAIL'}** (the smoke trade waits for Damian).", ""]
    args.out.write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
