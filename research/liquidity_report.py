"""Phase 2 liquidity report: BTC_USDC (linear) against inverse BTC options, for v1 spreads.

Reads exported days synced from R2 (``uv run optlab-data sync ...``) through MarketDataView and
writes ``research/liquidity_report.md``. The measurements are in
``optlab.evaluation.liquidity``. A hand-written "## Recommendation" section already in the
report is kept when it is regenerated.

    uv run python research/liquidity_report.py --data data --every 5
"""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from optlab.domain.clock import MS_PER_DAY, MS_PER_MINUTE, datetime_to_ms
from optlab.evaluation import liquidity as liq
from optlab.market_data.export import combined_version, manifest_path, read_manifest
from optlab.market_data.view import MarketDataView, ParquetSource, snapshot_times

REPORT = Path(__file__).with_name("liquidity_report.md")
MARKER = "\n## Recommendation"


def pct(x: float | None) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:.0%}"


def share(n: int, d: int) -> str:
    return "-" if not d else f"{n / d:.0%} ({n:,} of {d:,})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--every", type=int, default=5, help="sample every N snapshot minutes")
    args = parser.parse_args()

    source = ParquetSource.from_data_dir(args.data)
    days = source.days()
    if not days:
        raise SystemExit(f"no exported days under {args.data}: run optlab-data sync first")
    versions = [
        read_manifest(manifest_path(source.root, d).read_bytes())["dataset_version"] for d in days
    ]
    start = datetime_to_ms(datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC))
    end = datetime_to_ms(datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=UTC))
    end += MS_PER_DAY - MS_PER_MINUTE
    every = list(snapshot_times(source, start, end, skip_flagged=False))
    usable = list(snapshot_times(source, start, end))
    sampled = usable[:: args.every]

    began = time.monotonic()
    parts: list[pd.DataFrame] = []
    for t in sampled:
        spreads = liq.candidate_spreads(liq.snapshot_frame(MarketDataView(source, t)))
        if not spreads.empty:
            parts.append(spreads)
    spreads = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    summaries = {b: liq.summarize(spreads, sampled, b) for b in liq.BOOKS}

    for day in days:
        day_start = datetime_to_ms(datetime(day.year, day.month, day.day, tzinfo=UTC))
        view = MarketDataView(source, day_start + MS_PER_DAY - 1)
        trades = view.trades(day_start).to_pandas()
        books = view.instruments().to_pandas()[["instrument_name", "book", "kind"]]
        for b, summary in summaries.items():
            names = set(
                books[(books["book"] == b) & (books["kind"] == "option")]["instrument_name"]
            )
            liq.add_trades(summary, trades, liq.leg_names(spreads, b), names)

    first = datetime.fromtimestamp(usable[0] / 1000, UTC) if usable else None
    last = datetime.fromtimestamp(usable[-1] / 1000, UTC) if usable else None
    lines = [
        "# Liquidity report: BTC_USDC against inverse BTC options (Phase 2)",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `research/liquidity_report.py` "
        f"in {time.monotonic() - began:.0f} s.",
        "",
        f"- Data: {len(days)} exported UTC days, {days[0]} to {days[-1]} "
        f"(dataset version {combined_version(versions)[:16]}).",
        f"- Snapshot minutes: {len(every):,} recorded, {len(usable):,} usable after the "
        f"data-quality skips, every {args.every}th sampled: {len(sampled):,} minutes"
        + (
            f" from {first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC." if first and last else "."
        ),
        "- Candidate spreads follow the v1 entry rules: 7 to 30 days to expiry; short put delta "
        "-0.30 to -0.15 (bull put) or short call delta 0.15 to 0.30 (bear call), Deribit's "
        "delta; long leg the nearest further strike 2% to 6% of the forward away.",
        "- Liquidity rule (v1): both legs two-sided, bid-ask at most 10% of mid, quotes under "
        "2 minutes old, top-of-book size at least the order size. Order size: maximum loss "
        "(width minus credit) at most 2% of 100,000 USDC. Inverse premiums in USD at the "
        "index price; credit before fees.",
        "",
        "## Results",
        "",
        "| Measure | " + " | ".join(liq.BOOK_NAMES[b] for b in liq.BOOKS) + " |",
        "| --- | --- | --- |",
    ]

    def row(label: str, values: list[str]) -> None:
        lines.append(f"| {label} | " + " | ".join(values) + " |")

    s = [summaries[b] for b in liq.BOOKS]
    row("Expiries in the 7-30 day window", [", ".join(x.expiries) or "none" for x in s])
    row(
        "Minutes with any candidate spread", [share(x.minutes_with_candidate, x.minutes) for x in s]
    )
    row(
        "**Minutes with a spread passing the liquidity rule**",
        [share(x.minutes_liquid, x.minutes) for x in s],
    )
    row("... bull put side", [share(x.minutes_liquid_bull_put, x.minutes) for x in s])
    row("... bear call side", [share(x.minutes_liquid_bear_call, x.minutes) for x in s])
    row(
        "... and credit at least 20% of width (before fees)",
        [share(x.minutes_liquid_and_credit, x.minutes) for x in s],
    )
    row("Candidate spreads (spread-minutes)", [f"{x.spreads:,}" for x in s])
    for check, label in (
        ("two_sided", "both legs two-sided"),
        ("tight", "both legs bid-ask at most 10% of mid"),
        ("fresh", "both quotes under 2 minutes old"),
        ("deep", "top-of-book size covers the order"),
        ("liquid", "**all four (liquidity rule)**"),
        ("credit_ok", "credit at least 20% of width"),
    ):
        row(f"Share of spreads: {label}", [pct(x.share.get(check)) for x in s])
    m = [x.medians for x in s]
    row("Median bid-ask of short leg (of mid)", [pct(x.get("short_spread_of_mid")) for x in m])
    row("90th percentile, short leg", [pct(x.get("short_spread_of_mid_p90")) for x in m])
    row("Median bid-ask of long leg (of mid)", [pct(x.get("long_spread_of_mid")) for x in m])
    row("90th percentile, long leg", [pct(x.get("long_spread_of_mid_p90")) for x in m])
    row(
        "Median short-leg bid size (BTC)",
        [f"{x.get('short_bid_size', float('nan')):.2f}" for x in m],
    )
    row(
        "Median long-leg ask size (BTC)", [f"{x.get('long_ask_size', float('nan')):.2f}" for x in m]
    )
    row(
        "Median order size allowed by sizing (BTC)",
        [f"{x.get('order_btc', float('nan')):.2f}" for x in m],
    )
    row("Median width (of forward)", [pct(x.get("width_of_forward")) for x in m])
    row(
        "Median credit per BTC (USD, before fees)",
        [f"{x.get('credit_usd', float('nan')):,.0f}" for x in m],
    )
    row(
        "Trades on candidate legs",
        [f"{x.trades:,} ({x.trade_btc:,.1f} BTC, {x.trade_instruments} instruments)" for x in s],
    )
    row("All option trades in the book", [f"{x.book_trades:,}" for x in s])
    body = "\n".join(lines) + "\n"

    kept = ""
    if REPORT.exists() and MARKER in REPORT.read_text(encoding="utf-8"):
        kept = MARKER + REPORT.read_text(encoding="utf-8").split(MARKER, 1)[1]
    REPORT.write_text(body + kept, encoding="utf-8")
    print(body)


if __name__ == "__main__":
    main()
