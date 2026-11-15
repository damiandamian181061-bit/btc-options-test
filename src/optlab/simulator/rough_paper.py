"""ROUGH (Phase 2B) paper service: ``optlab-paper-rough``.

Runs the rough credit-spread rule (``optlab.strategy.rough``) on the live recorded data, every
minute, with paper fills only. It sends no orders anywhere, needs no keys and opens no network
connection except to the database. Its profit and loss is not a result (docs/rough-version.md).

For each new snapshot minute, read through ``MarketDataView(as_of=now)``:

1. Skip the minute if it is flagged: marked to skip by the hourly data checks, or, checked here
   on the spot, a partial snapshot or a missing or stale index (the hourly checks see the
   current minute only an hour later).
2. Settle every open spread whose expiry has passed, once the delivery price is known.
3. Close any open spread whose executable buy-back price hits the take profit or the stop.
4. Once per UTC day, at the first usable minute from 09:00 UTC: the daily decision. Every
   decision is stored with its reason, "no trade" included.
5. Record cash, the open spreads at Deribit's mark price, and equity.

    optlab-paper-rough          run forever (the ``paper`` service)
    optlab-paper-rough --once   process the latest snapshot once and exit

Loads ``optlab.config.Settings`` first, so it refuses to start with LIVE_TRADING=true.
"""

import argparse
import logging
import signal
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from optlab.config import Settings
from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR, floor_ms, ms_to_datetime
from optlab.domain.money import dec
from optlab.ledger.rough import (
    SpreadLegs,
    entry_fills,
    exit_fills,
    intrinsic,
    open_value,
    settlement_fills,
)
from optlab.market_data.quality import QualityConfig, load_quality_config
from optlab.market_data.store import SCHEMA_REVISION
from optlab.market_data.view import DatabaseSource, MarketDataSource, MarketDataView, snapshot_times
from optlab.risk.rough import OpenRisk, RoughLimits, approve, load_limits
from optlab.simulator import rough_store as store
from optlab.simulator.rough_fees import FeeSchedule, load_fee_schedule
from optlab.strategy.rough import (
    OptionQuote,
    RoughRule,
    buyback_price,
    candidates,
    exit_reason,
    load_rule,
    rank,
    signals,
    thesis,
)

logger = logging.getLogger("optlab.paper")


class PaperSettings(BaseSettings):
    """Non-secret settings for the paper service (``OPTLAB_`` prefix)."""

    model_config = SettingsConfigDict(env_prefix="OPTLAB_", extra="ignore", frozen=True)

    config_dir: Path = Path("configs")
    paper_poll_s: float = 10.0
    log_level: str = "INFO"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _when(ts_ms: int) -> str:
    return f"{ms_to_datetime(ts_ms):%Y-%m-%d %H:%M} UTC"


def _num(value: Decimal) -> str:
    """A decimal without trailing zeros or an exponent: 440.0 -> 440, 441.06000 -> 441.06."""
    return format(value.normalize(), "f")


def book_chain(view: MarketDataView, book: str, ts_ms: int) -> tuple[list[OptionQuote], int, int]:
    """The book's options at the snapshot, plus all option rows and all options listed."""
    rows = view.option_chain().to_pylist()
    instruments = {
        r["instrument_name"]: r for r in view.instruments().to_pylist() if r["kind"] == "option"
    }
    listed = sum(1 for r in instruments.values() if r["expiry_ms"] > ts_ms)
    out: list[OptionQuote] = []
    for r in rows:
        i = instruments.get(r["instrument_name"])
        if i is None or i["book"] != book:
            continue
        out.append(
            OptionQuote(
                instrument_name=r["instrument_name"],
                option_type=i["option_type"],
                strike=float(i["strike"]),
                expiry_ms=int(i["expiry_ms"]),
                bid=r["bid"],
                ask=r["ask"],
                bid_size=float(r["bid_size"]),
                ask_size=float(r["ask_size"]),
                mark_price=r["mark_price"],
                mark_iv=r["mark_iv"],
                delta=r["delta"],
                underlying_price=r["underlying_price"],
                index_price=r["index_price"],
                exchange_ts_ms=int(r["exchange_ts_ms"]),
                min_trade_amount=float(i["min_trade_amount"]),
            )
        )
    return out, len(rows), listed


def minute_flags(
    view: MarketDataView, ts_ms: int, option_rows: int, listed: int, quality: QualityConfig
) -> list[str]:
    """Why this snapshot minute must be skipped (global flags only); empty if it is usable."""
    flags: list[str] = []
    if not list(snapshot_times(view.source, ts_ms, ts_ms)):
        flags.append("marked to skip by the data checks")
    if listed and option_rows < quality.partial_snapshot_fraction * listed:
        flags.append(f"partial snapshot: {option_rows} of {listed} listed options")
    indexes = {
        r["instrument_name"]: r for r in view.underlyings().to_pylist() if r["kind"] == "index"
    }
    for name in quality.indexes:
        row = indexes.get(name)
        if row is None or row["index_price"] is None:
            flags.append(f"index {name} missing")
        elif ts_ms - int(row["exchange_ts_ms"]) > quality.index_stale_s * 1000:
            flags.append(f"index {name} older than {quality.index_stale_s} s")
    return flags


@dataclass
class RoughPaper:
    """One paper account running the rough rule. ``step`` is called every few seconds."""

    source: MarketDataSource
    conn: psycopg.Connection[Any]
    rule: RoughRule
    limits: RoughLimits
    fees: FeeSchedule
    quality: QualityConfig
    run_id: str
    started_ms: int
    last_ts: int | None = None

    def close(self) -> None:
        if isinstance(self.source, DatabaseSource):
            self.source.close()
        self.conn.close()

    def step(self, now: int) -> list[str]:
        """Process the latest snapshot if it is new; return what happened (also logged)."""
        view = MarketDataView(self.source, as_of=now)
        ts = view.snapshot_ms()
        if ts is None:
            note = "no snapshot in the last 5 minutes"
            store.heartbeat(self.conn, self.run_id, self.started_ms, now, note=note)
            return [note]
        if self.last_ts is not None and ts <= self.last_ts:
            return []
        self.last_ts = ts
        # Every read below is as of the snapshot minute itself, so a minute written meanwhile
        # can't mix in (and a delivery price recorded after ts waits for the next minute).
        view = MarketDataView(self.source, as_of=ts)
        chain, option_rows, listed = book_chain(view, self.rule.book, ts)
        flags = minute_flags(view, ts, option_rows, listed, self.quality)
        if flags:
            note = f"skipped {_when(ts)}: " + "; ".join(flags)
            logger.warning(note)
            store.heartbeat(
                self.conn,
                self.run_id,
                self.started_ms,
                now,
                snapshot_ms=ts,
                skipped=True,
                note=note,
            )
            return [note]
        by_name = {q.instrument_name: q for q in chain}
        notes = self._settle(view, ts, now)
        notes += self._exits(ts, now, by_name)
        day = ms_to_datetime(ts).date()
        due = floor_ms(ts, MS_PER_DAY) + self.rule.decision_hour_utc * MS_PER_HOUR
        if ts >= due and not store.has_decision(self.conn, day):
            notes.append(self._decide(view, ts, now, chain, by_name))
        notes += self._equity(ts, by_name)
        for note in notes:
            logger.info(note)
        store.heartbeat(
            self.conn, self.run_id, self.started_ms, now, snapshot_ms=ts,
            note=notes[-1] if notes else f"processed {_when(ts)}",
        )  # fmt: skip
        return notes

    # ---------------------------------------------------------------------------- settlement

    def _settle(self, view: MarketDataView, ts: int, now: int) -> list[str]:
        state = store.load_state(self.conn, self.limits.starting_equity)
        due = [s for s in state.open if s.legs.expiry_ms <= ts]
        if not due:
            return []
        prices = {
            r["delivery_date"]: Decimal(r["delivery_price"])
            for r in view.delivery_prices().to_pylist()
            if r["index_name"] == self.rule.index_name
        }
        notes: list[str] = []
        for s in due:
            price = prices.get(ms_to_datetime(s.legs.expiry_ms).date())
            if price is None:
                continue  # the recorder fetches it at 08:01; try again next minute
            fills = settlement_fills(s.legs, price, self.fees)
            short_value = intrinsic(s.legs.option_type, s.legs.short_strike, price)
            owed = short_value - intrinsic(s.legs.option_type, s.legs.long_strike, price)
            pnl = store.save_close(
                self.conn, s, status="settled", reason="settled at the delivery price",
                exit_price=owed, delivery_price=price, thesis_met=short_value == 0, fills=fills,
                ts_ms=ts, now_ms=now,
            )  # fmt: skip
            if pnl is not None:
                notes.append(
                    f"settled spread {s.id} at delivery price {_num(price)}: P&L {pnl:,.2f} USDC"
                )
        return notes

    # ---------------------------------------------------------------------------- exits

    def _exits(self, ts: int, now: int, by_name: dict[str, OptionQuote]) -> list[str]:
        state = store.load_state(self.conn, self.limits.starting_equity)
        notes: list[str] = []
        for s in state.open:
            if s.legs.expiry_ms <= ts:
                continue
            short, long = by_name.get(s.legs.short_name), by_name.get(s.legs.long_name)
            if short is None or long is None:
                continue
            price, _why = buyback_price(short, long, s.legs.amount, ts, self.rule)
            index = short.index_price or long.index_price
            if price is None or index is None or short.ask is None or long.bid is None:
                continue
            reason = exit_reason(s.entry_credit, price, self.rule)
            if reason is None:
                continue
            fills = exit_fills(
                s.legs, dec(short.ask), dec(long.bid), dec(index), self.fees, at_ms=ts
            )
            pnl = store.save_close(
                self.conn, s, status="closed", reason=reason, exit_price=price,
                delivery_price=None, thesis_met=reason == "take profit", fills=fills, ts_ms=ts,
                now_ms=now,
            )  # fmt: skip
            if pnl is not None:
                notes.append(
                    f"{reason}: bought back spread {s.id} at {_num(price)} per BTC "
                    f"(credit {_num(s.entry_credit)}): P&L {pnl:,.2f} USDC"
                )
        return notes

    # ---------------------------------------------------------------------------- decision

    def _decide(
        self,
        view: MarketDataView,
        ts: int,
        now: int,
        chain: Sequence[OptionQuote],
        by_name: dict[str, OptionQuote],
    ) -> str:
        rule = self.rule
        delivery = [
            (r["delivery_date"], Decimal(r["delivery_price"]))
            for r in view.delivery_prices().to_pylist()
            if r["index_name"] == rule.index_name
        ]
        index_price = next(
            (
                r["index_price"]
                for r in view.underlyings().to_pylist()
                if r["kind"] == "index" and r["instrument_name"] == rule.index_name
            ),
            None,
        )
        sig = signals(chain, delivery, index_price, ts, rule)
        d = store.DecisionRow(
            day=ms_to_datetime(ts).date(), ts_ms=ts, decided_ms=now, iv30=sig.iv30,
            rv30=sig.rv30, index_price=sig.index_price, trend_average=sig.trend_average,
            side=sig.side, short_legs=0, passed=0, action="no trade", reason=sig.reason,
        )  # fmt: skip

        def no_trade(reason: str, row: store.DecisionRow = d) -> str:
            store.save_no_trade(self.conn, replace(row, reason=reason))
            return f"decision for {row.day}: no trade: {reason}"

        if sig.side is None:
            return no_trade(sig.reason)
        in_window = {
            o.expiry_ms
            for o in chain
            if rule.dte_min_days <= (o.expiry_ms - ts) / MS_PER_DAY <= rule.dte_max_days
        }
        if not in_window:
            window = f"{rule.dte_min_days:.0f} to {rule.dte_max_days:.0f}"
            return no_trade(f"no BTC_USDC expiry is {window} days away")
        screen = candidates(chain, sig.side, ts, rule, self.fees)
        ranked = rank(screen.candidates, rule)
        d = replace(d, short_legs=screen.short_legs, passed=len(ranked))
        if not ranked:
            return no_trade(f"no {sig.side} spread passes every check: {screen.summary()}", d)

        state = store.load_state(self.conn, self.limits.starting_equity)
        marked = self._open_value(state.open, by_name, ts)
        if marked is None:
            return no_trade("an open spread has no mark price, so equity is unknown", d)
        equity = state.cash + marked
        open_risk = [OpenRisk(s.legs.expiry_ms, s.max_loss) for s in state.open]
        rejected: list[str] = []
        for c in ranked:
            a = approve(c, equity, open_risk, self.limits)
            if a.amount is None or a.max_loss is None:
                rejected.append(f"{c.short.instrument_name}: {a.reason}")
                continue
            credit, net, index = c.credit, c.credit_after_fees, c.short.index_price
            if credit is None or net is None or index is None or not c.short.bid or not c.long.ask:
                continue  # cannot happen: a passing candidate has both prices and an index
            legs = SpreadLegs(
                sig.side, c.expiry_ms, c.short.instrument_name, c.long.instrument_name,
                dec(c.short.strike), dec(c.long.strike), a.amount,
            )  # fmt: skip
            fills = entry_fills(
                legs, dec(c.short.bid), dec(c.long.ask), dec(index), self.fees, at_ms=ts
            )
            reason = (
                f"sold {a.amount} BTC {sig.side} {c.short.instrument_name} / "
                f"{c.long.instrument_name} (short delta {c.short.delta:+.3f}): credit "
                f"{_num(credit)} per BTC, {_num(net)} after fees; max loss "
                f"{a.max_loss:,.2f} USDC"
            )
            if rejected:
                reason += "; closer to 0.20 but refused by the limits: " + "; ".join(rejected)
            row = replace(d, action="entered", reason=reason)
            spread_id = store.save_entry(
                self.conn, row, legs, short_delta=float(c.short.delta or 0.0),
                entry_credit=credit, credit_after_fees=net,
                max_loss=a.max_loss, thesis=thesis(sig.side, c.short.strike, c.expiry_ms),
                fills=fills,
            )  # fmt: skip
            return f"decision for {d.day}: entered spread {spread_id}: {reason}"
        return no_trade("every passing spread is refused by the limits: " + "; ".join(rejected), d)

    # ---------------------------------------------------------------------------- equity

    @staticmethod
    def _open_value(
        spreads: Sequence[store.OpenSpread], by_name: dict[str, OptionQuote], ts: int
    ) -> Decimal | None:
        total = Decimal(0)
        for s in spreads:
            short, long = by_name.get(s.legs.short_name), by_name.get(s.legs.long_name)
            if short is None or long is None or short.mark_price is None or long.mark_price is None:
                return None
            total += open_value(s.legs, dec(short.mark_price), dec(long.mark_price))
        return total

    def _equity(self, ts: int, by_name: dict[str, OptionQuote]) -> list[str]:
        state = store.load_state(self.conn, self.limits.starting_equity)
        marked = self._open_value(state.open, by_name, ts)
        if marked is None:
            return [f"no equity row for {_when(ts)}: an open leg has no mark price"]
        max_loss = sum((s.max_loss for s in state.open), Decimal(0))
        store.save_equity(self.conn, ts, state.cash, marked, len(state.open), max_loss)
        return []


# ------------------------------------------------------------------------------------ service


def _database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set: the paper service needs the database")
    return settings.database_url.get_secret_value()


def _check_schema(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or row[0] != SCHEMA_REVISION:
        raise SystemExit(
            f"database schema is at {row[0] if row else None!r}, the paper service needs "
            f"{SCHEMA_REVISION!r}: back up, then run the migrations (infra/server-setup.md)"
        )


def build(url: str, paper: PaperSettings, *, started_ms: int) -> RoughPaper:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    return RoughPaper(
        source=DatabaseSource(url),
        conn=store.connect(url),
        rule=load_rule(paper.config_dir),
        limits=load_limits(paper.config_dir),
        fees=load_fee_schedule(paper.config_dir),
        quality=load_quality_config(paper.config_dir),
        run_id=run_id,
        started_ms=started_ms,
    )


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()  # refuses LIVE_TRADING=true
    paper = PaperSettings()
    parser = argparse.ArgumentParser(
        prog="optlab-paper-rough", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--once", action="store_true", help="process the latest snapshot and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=paper.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime
    url = _database_url(settings)
    _check_schema(url)
    service = build(url, paper, started_ms=now_ms())
    logger.info(
        "ROUGH paper service %s starting: paper fills only, no orders sent anywhere; "
        "decision daily from %02d:00 UTC",
        service.run_id,
        service.rule.decision_hour_utc,
    )
    if args.once:
        for note in service.step(now_ms()) or ["nothing new"]:
            print(note)
        return 0
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop like Ctrl-C
    try:
        while True:
            try:
                service.step(now_ms())
            except (psycopg.Error, OSError):
                logger.exception("database error; reconnecting")
                _reconnect(service, url)
            time.sleep(paper.paper_poll_s)
    except KeyboardInterrupt:
        logger.info("stopped by signal")
    return 0


def _reconnect(service: RoughPaper, url: str) -> None:
    with suppress(Exception):  # best effort: the old connections are already broken
        if isinstance(service.source, DatabaseSource):
            service.source.close()
    with suppress(Exception):
        service.conn.close()
    try:
        service.source = DatabaseSource(url)
        service.conn = store.connect(url)
    except psycopg.Error:
        logger.exception("reconnect failed; trying again next loop")
