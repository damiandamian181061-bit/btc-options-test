"""One book's options, futures, index and perpetual at a snapshot, as the surface code wants them.

Built only from what ``MarketDataView`` returns at the snapshot (``option_chain``,
``underlyings``, ``instruments``), so nothing after the snapshot is read. Prices stay in their
own currency: BTC premiums for inverse options, USDC for linear options and futures, USD for
inverse futures and the ``btc_usd`` index.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd
import pyarrow as pa

from optlab.domain.market import Book

INDEX_NAME = {Book.LINEAR: "btc_usdc", Book.INVERSE: "btc_usd"}
PERPETUAL_NAME = {Book.LINEAR: "BTC_USDC-PERPETUAL", Book.INVERSE: "BTC-PERPETUAL"}

QUOTE_COLUMNS = [
    "instrument_name",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
    "mark_price",
    "mark_iv",
    "delta",
    "gamma",
    "vega",
    "theta",
    "underlying_price",
    "index_price",
    "open_interest",
    "state",
    "exchange_ts_ms",
]
CHAIN_COLUMNS = ["instrument_name", "expiry_ms", "strike", "call", "tick_size", *QUOTE_COLUMNS[1:]]
FUTURE_COLUMNS = ["instrument_name", "expiry_ms", "mark_price", "bid", "ask"]


def _frame(table: pa.Table | pd.DataFrame) -> pd.DataFrame:
    return table.to_pandas() if isinstance(table, pa.Table) else table


def option_chain(
    quotes: pa.Table | pd.DataFrame, instruments: pa.Table | pd.DataFrame, book: Book
) -> pd.DataFrame:
    """The book's options at the snapshot, joined with their strike, expiry, type and tick."""
    q = _frame(quotes)
    ins = _frame(instruments)
    ins = ins.loc[(ins["kind"] == "option") & (ins["book"] == book.value)]
    ins = ins[["instrument_name", "expiry_ms", "strike", "option_type", "tick_size"]]
    df = q[QUOTE_COLUMNS].merge(ins, on="instrument_name", how="inner")
    df["call"] = df["option_type"] == "call"
    df["expiry_ms"] = df["expiry_ms"].astype("int64")
    df["strike"] = df["strike"].astype(float)
    for col in ("bid", "ask", "mark_price", "mark_iv", "underlying_price", "index_price"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df[CHAIN_COLUMNS].sort_values(["expiry_ms", "strike", "call"]).reset_index(drop=True)


def futures(
    ticks: pa.Table | pd.DataFrame, instruments: pa.Table | pd.DataFrame, book: Book
) -> pd.DataFrame:
    """The book's dated futures at the snapshot: name, expiry, mark, bid and ask."""
    t = _frame(ticks)
    ins = _frame(instruments)
    ins = ins.loc[(ins["kind"] == "future") & (ins["book"] == book.value)]
    df = t.loc[t["kind"] == "future", ["instrument_name", "mark_price", "bid", "ask"]].merge(
        ins[["instrument_name", "expiry_ms"]], on="instrument_name", how="inner"
    )
    df["expiry_ms"] = df["expiry_ms"].astype("int64")
    for col in ("mark_price", "bid", "ask"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df[FUTURE_COLUMNS].sort_values("expiry_ms").reset_index(drop=True)


def _row(ticks: pa.Table | pd.DataFrame, name: str) -> dict[str, Any] | None:
    t = _frame(ticks)
    rows = t.loc[t["instrument_name"] == name]
    return None if rows.empty else rows.iloc[0].to_dict()


def _num(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def index_price(ticks: pa.Table | pd.DataFrame, book: Book) -> float | None:
    """The book's price index (``btc_usdc`` or ``btc_usd``) at the snapshot."""
    row = _row(ticks, INDEX_NAME[book])
    return None if row is None else _num(row.get("index_price"))


@dataclass(frozen=True, slots=True)
class Perpetual:
    mark_price: float | None
    funding_8h: float | None
    current_funding: float | None


def perpetual(ticks: pa.Table | pd.DataFrame, book: Book) -> Perpetual | None:
    """The book's perpetual (mark price and funding) at the snapshot."""
    row = _row(ticks, PERPETUAL_NAME[book])
    if row is None:
        return None
    return Perpetual(
        mark_price=_num(row.get("mark_price")),
        funding_8h=_num(row.get("funding_8h")),
        current_funding=_num(row.get("current_funding")),
    )
