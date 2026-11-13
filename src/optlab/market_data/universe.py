"""Which instruments the recorder follows, and when it looks for new ones.

Discovery asks Deribit for every active option and future settled in BTC and in USDC, then keeps:

- inverse BTC options (premium and settlement in BTC),
- BTC_USDC linear options (premium and settlement in USDC),
- the BTC perpetual of each book (BTC-PERPETUAL, BTC_USDC-PERPETUAL),
- the BTC dated futures of each book,
- the price index behind each of them (``btc_usd``, ``btc_usdc``).

Every choice is made from instrument metadata (base currency, kind, settlement currency, strike,
expiry, contract size); no instrument name is parsed.

Discovery runs every 10 minutes and again 30 seconds after each 08:00 UTC expiry, when Deribit
delists the expired instruments and lists new ones.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from optlab.domain.clock import MS_PER_DAY, MS_PER_HOUR, MS_PER_MINUTE, MS_PER_SECOND
from optlab.domain.market import Book, Instrument, InstrumentKind
from optlab.market_data.deribit import channels as ch

# (currency, kind) pairs passed to public/get_instruments; currency is the settlement currency.
DISCOVERY_QUERIES: tuple[tuple[str, str], ...] = (
    ("BTC", "option"),
    ("USDC", "option"),
    ("BTC", "future"),
    ("USDC", "future"),
)
BASE_CURRENCY = "BTC"
SETTLEMENT_CURRENCY = {Book.INVERSE: "BTC", Book.LINEAR: "USDC"}

EXPIRY_TIME_OF_DAY_MS = 8 * MS_PER_HOUR  # Deribit expiries are at 08:00 UTC
AFTER_EXPIRY_DELAY_MS = 30 * MS_PER_SECOND
DISCOVERY_INTERVAL_MS = 10 * MS_PER_MINUTE


def in_universe(instrument: Instrument) -> bool:
    """True for the BTC options, perpetuals and dated futures of the two books."""
    if not instrument.is_active or instrument.base_currency != BASE_CURRENCY:
        return False
    if instrument.settlement_currency != SETTLEMENT_CURRENCY[instrument.book]:
        return False
    if instrument.kind is InstrumentKind.OPTION:
        # Premiums in the settlement currency: BTC for inverse, USDC for linear.
        return (
            instrument.quote_currency == instrument.settlement_currency
            and instrument.strike is not None
            and instrument.expiry_ms is not None
        )
    if instrument.kind is InstrumentKind.FUTURE:
        return instrument.expiry_ms is not None
    return instrument.kind is InstrumentKind.PERPETUAL


@dataclass(frozen=True)
class Universe:
    """The instruments being recorded, keyed by name."""

    instruments: Mapping[str, Instrument] = field(default_factory=dict)

    @property
    def options(self) -> list[Instrument]:
        return [i for i in self.instruments.values() if i.kind is InstrumentKind.OPTION]

    @property
    def underlyings(self) -> list[Instrument]:
        return [i for i in self.instruments.values() if i.kind is not InstrumentKind.OPTION]

    def index_currencies(self) -> dict[str, str]:
        """Price index name to the currency it is quoted in, e.g. ``btc_usd`` to ``USD``."""
        return {i.price_index: i.counter_currency for i in self.instruments.values()}

    def channels(self, interval: str = "100ms") -> set[str]:
        """Every public channel needed to record this universe."""
        out = {ch.ticker(name, interval) for name in self.instruments}
        out |= {ch.price_index(name) for name in self.index_currencies()}
        out |= {ch.trades("option", i.settlement_currency, interval) for i in self.options}
        return out

    def diff(self, newer: "Universe") -> tuple[set[str], set[str]]:
        """Names (added, removed) going from this universe to ``newer``."""
        old, new = set(self.instruments), set(newer.instruments)
        return new - old, old - new


def build_universe(instruments: Iterable[Instrument]) -> Universe:
    return Universe({i.name: i for i in instruments if in_universe(i)})


def next_discovery_ms(
    now_ms: int,
    interval_ms: int = DISCOVERY_INTERVAL_MS,
    after_expiry_ms: int = AFTER_EXPIRY_DELAY_MS,
) -> int:
    """The next discovery time after ``now_ms``.

    Runs fall on a fixed grid (every ``interval_ms`` since midnight UTC, shifted by
    ``after_expiry_ms``) and always include 08:00 UTC plus ``after_expiry_ms``.
    """
    day = now_ms - now_ms % MS_PER_DAY
    periodic = now_ms + interval_ms - (now_ms - after_expiry_ms) % interval_ms
    after_expiry = day + EXPIRY_TIME_OF_DAY_MS + after_expiry_ms
    if after_expiry <= now_ms:
        after_expiry += MS_PER_DAY
    return min(periodic, after_expiry)
