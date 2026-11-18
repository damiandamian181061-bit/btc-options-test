"""Surface worker: ``optlab-surface``. Every new snapshot minute, for both books: the forwards,
smiles, surface buckets and features, stored in vol_expiries, vol_surface and features.

Everything for a minute is read through ``MarketDataView(as_of=minute)``, so nothing recorded
after the minute is used. It processes each new snapshot minute once; after a restart it carries
on from the latest minute (missed minutes are not filled in: the surface can be rebuilt from the
recorded quotes later if needed). It sends nothing anywhere, needs no keys and reaches only the
database.

    optlab-surface                     run forever (the ``surface`` service)
    optlab-surface --once              process the latest snapshot, store it, print a summary
    optlab-surface --once --dry-run    the same without storing anything

Loads ``optlab.config.Settings`` first, so it refuses to start with LIVE_TRADING=true, and
checks that the tables it uses are as this code expects (``optlab.market_data.schema``).
"""

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from optlab.config import Settings
from optlab.domain.clock import MS_PER_MINUTE, ms_to_datetime
from optlab.domain.market import Book
from optlab.market_data import schema
from optlab.market_data.view import DatabaseSource, MarketDataSource, MarketDataView
from optlab.volatility import chain as ch
from optlab.volatility import store
from optlab.volatility.features import Features, compute_features
from optlab.volatility.realised import (
    CLOSE_HOUR_UTC,
    DailyClose,
    close_ms,
    closes_known_at,
    merge_closes,
)
from optlab.volatility.surface import Bucket, Surface, SurfaceConfig, build_surface

logger = logging.getLogger("optlab.surface")

CLOSE_DAYS = 31  # closes kept: 30 daily returns
CLOSE_RETRY_MS = 10 * MS_PER_MINUTE


class SurfaceSettings(BaseSettings):
    """Non-secret settings for the surface worker (``OPTLAB_`` prefix)."""

    model_config = SettingsConfigDict(env_prefix="OPTLAB_", extra="ignore", frozen=True)

    surface_poll_s: float = 10.0
    log_level: str = "INFO"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def close_day(ts_ms: int) -> date:
    """The latest day whose 08:00 UTC close is at or before ``ts_ms``."""
    return ms_to_datetime(ts_ms - CLOSE_HOUR_UTC * 3_600_000).date()


def load_closes(
    source: MarketDataSource, ts_ms: int, books: Sequence[Book]
) -> dict[Book, list[DailyClose]]:
    """Daily closes per book known at ``ts_ms``: our 08:00 index snapshot, else the delivery
    price, for the last ``CLOSE_DAYS`` days."""
    last = close_day(ts_ms)
    days = [last - timedelta(days=i) for i in range(CLOSE_DAYS - 1, -1, -1)]
    own: dict[Book, dict[date, float]] = {b: {} for b in books}
    for d in days:
        at = close_ms(d)
        view = MarketDataView(source, at, max_age_ms=MS_PER_MINUTE)
        if view.snapshot_ms() is None:
            continue
        ticks = view.underlyings()
        for b in books:
            price = ch.index_price(ticks, b)
            if price is not None and price > 0:
                own[b][d] = price
    delivered = MarketDataView(source, ts_ms).delivery_prices().to_pylist()
    out: dict[Book, list[DailyClose]] = {}
    for b in books:
        delivery = {
            r["delivery_date"]: float(r["delivery_price"])
            for r in delivered
            if r["index_name"] == ch.INDEX_NAME[b] and days[0] <= r["delivery_date"] <= last
        }
        out[b] = closes_known_at(merge_closes(own[b], delivery), ts_ms)
    return out


@dataclass
class CloseCache:
    """Closes reloaded once a day, and every 10 minutes while today's close is still missing."""

    books: Sequence[Book]
    closes: dict[Book, list[DailyClose]] = field(default_factory=dict)
    day: date | None = None
    loaded_ms: int = 0

    def get(self, source: MarketDataSource, ts_ms: int) -> dict[Book, list[DailyClose]]:
        today = close_day(ts_ms)
        complete = all(c and c[-1].day == today for c in self.closes.values()) and bool(self.closes)
        stale = not complete and ts_ms - self.loaded_ms >= CLOSE_RETRY_MS
        if self.day != today or stale or ts_ms < self.loaded_ms:
            self.closes = load_closes(source, ts_ms, self.books)
            self.day, self.loaded_ms = today, ts_ms
        return {b: closes_known_at(c, ts_ms) for b, c in self.closes.items()}


@dataclass(frozen=True)
class Minute:
    ts_ms: int
    surfaces: dict[Book, Surface]
    features: dict[Book, Features]
    option_rows: int


def compute_minute(
    source: MarketDataSource,
    as_of_ms: int,
    closes: CloseCache,
    *,
    books: Sequence[Book] = (Book.LINEAR, Book.INVERSE),
    config: SurfaceConfig = SurfaceConfig(),  # noqa: B008 (frozen, safe as a default)
) -> Minute | None:
    """Surface and features of every book at the latest snapshot at or before ``as_of_ms``."""
    snap = MarketDataView(source, as_of_ms).snapshot_ms()
    if snap is None:
        return None
    view = MarketDataView(source, snap)  # everything as known at the snapshot minute itself
    quotes = view.option_chain()
    ticks = view.underlyings()
    instruments = view.instruments()
    known = closes.get(source, snap)
    surfaces: dict[Book, Surface] = {}
    features: dict[Book, Features] = {}
    for book in books:
        idx = ch.index_price(ticks, book)
        surface = build_surface(
            ch.option_chain(quotes, instruments, book),
            ch.futures(ticks, instruments, book),
            book,
            snap,
            index_price=idx,
            config=config,
        )
        surfaces[book] = surface
        features[book] = compute_features(surface, known[book], idx, ch.perpetual(ticks, book))
    return Minute(snap, surfaces, features, quotes.num_rows)


def summary(minute: Minute) -> list[str]:
    when = f"{ms_to_datetime(minute.ts_ms):%Y-%m-%d %H:%M} UTC"
    lines = [f"surface at {when} ({minute.option_rows} option rows)"]
    for book, s in minute.surfaces.items():
        f = minute.features[book]
        used = [x for x in s.fits if x.used]
        bad_fly = sum(1 for x in s.fits if x.butterfly_min is not None and x.butterfly_min < 0)
        bad_cal = sum(1 for x in s.fits if x.calendar_ok is False)

        def pct(v: float | None) -> str:
            return "-" if v is None else f"{v * 100:.2f}%"

        lines.append(
            f"  {book.value}: {len(s.fits)} expiries ({len(used)} used), {len(s.points)} surface "
            f"points; ATM 7d {pct(f.atm_iv_7d)}, 30d {pct(f.atm_iv_30d)}, 90d {pct(f.atm_iv_90d)}; "
            f"RR25 30d {pct(f.rr25_30d)}; RV30 {pct(f.rv_30d)} ({f.rv_30d_returns} returns, "
            f"{f.rv_30d_delivery} delivery closes); basis 30d {pct(f.basis_30d)}; "
            f"butterfly < 0: {bad_fly}; calendar breaks: {bad_cal}"
        )
        for x in s.fits:
            fw = x.forward
            diff = "-" if fw.difference_bp is None else f"{fw.difference_bp:+.1f} bp"
            atm = x.bucket_iv.get(Bucket.ATM)
            lines.append(
                f"    {ms_to_datetime(x.expiry_ms):%d %b %y} {x.tau * 365:6.2f} d  "
                f"F {fw.forward or 0:,.2f} ({fw.source.value}, parity {diff})  "
                f"{x.n_points:3d} quotes  ATM {pct(atm)}  {x.note}"
            )
    return lines


@dataclass
class SurfaceWorker:
    source: MarketDataSource
    conn: psycopg.Connection[Any] | None  # None: dry run
    closes: CloseCache
    config: SurfaceConfig = SurfaceConfig()  # noqa: RUF009 (frozen, safe as a default)
    last_ms: int | None = None

    def step(self, now: int) -> Minute | None:
        """Process the latest snapshot if it is new; return it (None if nothing new)."""
        latest = MarketDataView(self.source, now).snapshot_ms()
        if latest is None or (self.last_ms is not None and latest <= self.last_ms):
            return None
        minute = compute_minute(
            self.source, latest, self.closes, books=self.closes.books, config=self.config
        )
        if minute is None:
            return None
        if self.conn is not None:
            store.save_minute(
                self.conn, minute.surfaces.values(), minute.features.values(), computed_ms=now_ms()
            )
        self.last_ms = minute.ts_ms
        return minute


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()  # refuses LIVE_TRADING=true
    worker_settings = SurfaceSettings()
    parser = argparse.ArgumentParser(prog="optlab-surface", description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="process the latest snapshot and exit")
    parser.add_argument("--dry-run", action="store_true", help="with --once: store nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=worker_settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set: the surface worker needs the database")
    url = settings.database_url.get_secret_value()
    tables = (*schema.VIEW_TABLES, *schema.SURFACE_TABLES)
    revision = schema.check_url(url, tables, "the surface worker")
    logger.info(
        "surface worker starting (database revision %s); both books, every minute", revision
    )
    books = (Book.LINEAR, Book.INVERSE)
    dry = args.once and args.dry_run
    worker = SurfaceWorker(
        source=DatabaseSource(url),
        conn=None if dry else store.connect(url),
        closes=CloseCache(books),
    )
    if worker.conn is not None:
        worker.last_ms = store.last_minute(worker.conn)
    if args.once:
        worker.last_ms = None
        minute = worker.step(now_ms())
        for line in summary(minute) if minute else ["no snapshot in the last 5 minutes"]:
            print(line)
        return 0
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop like Ctrl-C
    try:
        while True:
            try:
                minute = worker.step(now_ms())
                if minute is not None and minute.ts_ms % (60 * MS_PER_MINUTE) == 0:
                    for line in summary(minute)[:3]:
                        logger.info(line)
            except (psycopg.Error, OSError):
                logger.exception("database error; reconnecting")
                _reconnect(worker, url)
            except Exception:  # a bad minute must not stop the worker: log it and go on
                logger.exception("could not build the surface; skipping this minute")
                with suppress(Exception):
                    latest = MarketDataView(worker.source, now_ms()).snapshot_ms()
                    worker.last_ms = latest
            time.sleep(worker_settings.surface_poll_s)
    except KeyboardInterrupt:
        logger.info("stopped by signal")
    return 0


def _reconnect(worker: SurfaceWorker, url: str) -> None:
    with suppress(Exception):  # best effort: the old connections are already broken
        if isinstance(worker.source, DatabaseSource):
            worker.source.close()
    with suppress(Exception):
        if worker.conn is not None:
            worker.conn.close()
    try:
        worker.source = DatabaseSource(url)
        worker.conn = store.connect(url)
    except psycopg.Error:
        logger.exception("reconnect failed; trying again next loop")
