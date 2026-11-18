"""ROUGH (Phase 2B) paper service, end to end on a made-up market in a test database.

The market is the strategy tests' chain (BTC_USDC, forward 65,000, index 64,900): ATM mark IV
0.45 on 27 Nov and 0.40 on 25 Dec (IV30 about 0.41), BTC falling 1% a day for 31 days (RV30
0.19, BTC below its 20-day average), so the rule sells a bear call. Values are invented.
"""

import ast
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from optlab.domain.clock import MS_PER_MINUTE, ms_to_datetime
from optlab.market_data.quality import load_quality_config
from optlab.market_data.rows import OptionQuoteRow, UnderlyingTickRow
from optlab.market_data.view import DatabaseSource
from optlab.risk.rough import load_limits
from optlab.simulator import rough_paper, rough_store
from optlab.simulator.rough_fees import load_fee_schedule
from optlab.simulator.rough_paper import RoughPaper
from optlab.strategy.rough import OptionQuote, load_rule
from tests.conftest import REPO_ROOT
from tests.market_data.synthetic import INSTRUMENT_COLUMNS, copy_rows, utc_ms
from tests.rough.test_strategy import E1, E2, calls, history, q

D = Decimal
M = MS_PER_MINUTE
T9 = utc_ms(2026, 11, 15, 9)
LISTED = utc_ms(2026, 11, 1)


def base_chain() -> list[OptionQuote]:
    """E1 calls for the bear call, plus the strikes around the forward for the ATM IVs."""
    return [
        *calls(),
        q(64_000, "put", E1, delta=0.4, iv=0.45),
        q(64_000, "put", E2, delta=0.4, iv=0.40, bid=1900, ask=1950),
        q(66_000, "call", E2, delta=0.45, iv=0.40, bid=2900, ask=2950),
    ]


def instrument_row(o: OptionQuote, i: int) -> tuple[Any, ...]:
    return (
        o.instrument_name, 5000 + i, "option", "linear", "BTC", "USDC", "USDC", "USDC",
        "btc_usdc", o.option_type, o.strike, o.expiry_ms, 1.0, 5.0, 0.01, "week",
        LISTED, LISTED, LISTED,
    )  # fmt: skip


def quote_row(ts: int, o: OptionQuote) -> OptionQuoteRow:
    return OptionQuoteRow(
        ts=ms_to_datetime(ts), instrument_name=o.instrument_name, currency="USDC", bid=o.bid,
        bid_size=o.bid_size, ask=o.ask, ask_size=o.ask_size, mark_price=o.mark_price,
        mark_iv=o.mark_iv, bid_iv=None, ask_iv=None, delta=o.delta, gamma=0.00001, vega=40.0,
        theta=-90.0, rho=1.0, open_interest=10.0, underlying_price=o.underlying_price,
        underlying_index="BTC_USDC-27NOV26", index_price=o.index_price, interest_rate=0.0,
        last_price=None, state="open", exchange_ts_ms=ts - 500, recv_ts_ms=ts - 450,
    )  # fmt: skip


def index_rows(
    ts: int, names: tuple[str, ...] = ("btc_usd", "btc_usdc")
) -> list[UnderlyingTickRow]:
    return [
        UnderlyingTickRow(
            ms_to_datetime(ts), name, "index", "USD" if name == "btc_usd" else "USDC",
            None, None, None, None, None, None, 64_900.0, None, None, None, None, None,
            ts - 500, ts - 450,
        )
        for name in names
    ]  # fmt: skip


def market(url: str, end: date = date(2026, 11, 15)) -> None:
    """Instruments and 31 days of btc_usdc delivery prices ending on ``end``."""
    with psycopg.connect(url) as conn:
        copy_rows(
            conn,
            "instruments",
            INSTRUMENT_COLUMNS,
            [instrument_row(o, i) for i, o in enumerate(base_chain())],
        )
        for day, price in history(85_000, 0.99, end=end):
            known = utc_ms(day.year, day.month, day.day, 8, 1)
            conn.execute(
                "INSERT INTO delivery_prices VALUES ('btc_usdc', %s, %s, 'USDC', %s)",
                (day, price, known),
            )


def minute(url: str, ts: int, chain: list[OptionQuote], *, indexes=("btc_usd", "btc_usdc")) -> None:
    with psycopg.connect(url) as conn:
        copy_rows(conn, "option_quotes", OptionQuoteRow._fields, [quote_row(ts, o) for o in chain])
        copy_rows(conn, "underlying_ticks", UnderlyingTickRow._fields, index_rows(ts, indexes))


OPENED: list[RoughPaper] = []


@pytest.fixture(autouse=True)
def _close_papers() -> Iterator[None]:
    yield
    while OPENED:
        OPENED.pop().close()


def paper(url: str) -> RoughPaper:
    configs = REPO_ROOT / "configs"
    p = RoughPaper(
        source=DatabaseSource(url),
        conn=rough_store.connect(url),
        rule=load_rule(configs),
        limits=load_limits(configs),
        fees=load_fee_schedule(configs),
        quality=load_quality_config(configs),
        run_id="test-run",
        started_ms=T9 - 10 * M,
    )
    OPENED.append(p)
    return p


def one(url: str, query: str, params: Any = None) -> Any:
    with psycopg.connect(url) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0] if len(row) == 1 else row


def cash(url: str) -> Decimal:
    return D("100000") + D(one(url, "SELECT coalesce(sum(cash), 0)::text FROM rough_fills"))


@pytest.mark.db
def test_enters_once_a_day_survives_a_restart_and_takes_profit(database_url: str) -> None:
    url = database_url
    market(url)
    minute(url, T9 - M, base_chain())
    minute(url, T9, base_chain())
    p = paper(url)

    assert p.step(T9 - M + 30_000) == []  # 08:59: before the decision time
    assert one(url, "SELECT count(*) FROM rough_decisions") == 0
    assert one(url, "SELECT equity FROM rough_equity") == D("100000")

    notes = p.step(T9 + 30_000)
    assert len(notes) == 1
    assert notes[0].startswith("decision for 2026-11-15: entered spread 1: sold 1.25 BTC bear call")
    assert "BTC_USDC-27NOV26-70000-C / BTC_USDC-27NOV26-72000-C" in notes[0]
    # Entry: +1.25 * (840 - 400) = 550, less fees 1.25 * 19.47 * 2 = 48.675.
    assert cash(url) == D("100501.325")
    # Equity at mark: 1.25 * (390 - 855) = -581.25 on the open spread.
    equity = one(url, "SELECT equity FROM rough_equity WHERE ts = %s", (ms_to_datetime(T9),))
    assert equity == D("99920.075")
    spread = one(
        url,
        "SELECT status, amount, entry_credit, max_loss, thesis FROM rough_spreads",
    )
    assert spread == (
        "open",
        D("1.25"),
        D("440"),
        D("1998.675"),
        "BTC stays below 70,000 until expiry on 27 Nov 2026 08:00 UTC; "
        "buy back at half the credit.",
    )
    decision = one(url, "SELECT action, side, short_legs, passed, spread_id FROM rough_decisions")
    assert decision == ("entered", "bear call", 4, 2, 1)

    assert p.step(T9 + 40_000) == []  # the same minute again: nothing
    restarted = paper(url)
    restarted.run_id = "test-run-2"
    restarted.step(T9 + 50_000)  # a new run reprocesses 09:00 but never decides twice
    assert one(url, "SELECT count(*) FROM rough_decisions") == 1
    assert one(url, "SELECT count(*) FROM rough_spreads") == 1

    # 09:01: the short call falls to 200/210 and the long call to 15/20. Buy-back 210 - 15 = 195,
    # at most 50% of the 440 credit: take profit.
    cheap: dict[float, tuple[float, float]] = {70_000: (200.0, 210.0), 72_000: (15.0, 20.0)}

    def fall(o: OptionQuote) -> OptionQuote:
        if o.expiry_ms != E1 or o.option_type != "call" or o.strike not in cheap:
            return o
        bid, ask = cheap[o.strike]
        return replace(o, bid=bid, ask=ask, mark_price=(bid + ask) / 2)

    chain = [fall(o) for o in base_chain()]
    minute(url, T9 + M, chain)
    notes = restarted.step(T9 + M + 30_000)
    assert notes[0].startswith("take profit: bought back spread 1 at 195 per BTC")
    # Exit: -1.25 * 210 - 24.3375 (fee 19.47/BTC) + 1.25 * 15 - 2.34375 (capped at 12.5% of 15).
    closed = one(url, "SELECT status, exit_reason, pnl, thesis_met FROM rough_spreads")
    assert closed == ("closed", "take profit", D("230.89375"), True)
    assert cash(url) == D("100230.89375")
    assert one(url, "SELECT count(*) FROM rough_fills") == 4
    runs = {"test-run": (2, 0), "test-run-2": (2, 0)}  # 08:59 and 09:00; 09:00 and 09:01
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT run_id, minutes, skipped FROM rough_runs").fetchall()
    assert {r: (m, k) for r, m, k in rows} == runs


@pytest.mark.db
def test_settles_at_the_delivery_price(database_url: str) -> None:
    url = database_url
    market(url)
    minute(url, T9, base_chain())
    p = paper(url)
    p.step(T9 + 30_000)
    # 27 Nov 08:01: the 27 Nov options have expired; delivery price 71,000 recorded at 08:01.
    t = utc_ms(2026, 11, 27, 8, 1)
    minute(url, t, [o for o in base_chain() if o.expiry_ms == E2])
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO delivery_prices VALUES ('btc_usdc', '2026-11-27', '71000', 'USDC', %s)",
            (t,),
        )
    notes = p.step(t + 30_000)
    assert notes == ["settled spread 1 at delivery price 71000: P&L -761.99 USDC"]
    # Short 70,000 call worth 1,000: -1.25 * 1,000 - delivery fee 1.25 * 10.65 = -1,263.3125.
    settled = one(
        url, "SELECT status, exit_price, delivery_price, pnl, thesis_met "
        "FROM rough_spreads",
    )  # fmt: skip
    assert settled == ("settled", D("1000"), D("71000"), D("-761.9875"), False)
    assert cash(url) == D("100000") + D("501.325") - D("1263.3125")


@pytest.mark.db
@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        # The 0.19-delta 70,000 call's bid down to 800: credit 400, 361.06 after fees, under
        # the 400 minimum. The next closest to 0.20 that passes (69,000 / 71,000) is chosen.
        (
            lambda o: replace(o, bid=800.0) if o.strike == 70_000 and o.expiry_ms == E1 else o,
            "entered spread 1: sold 1.28 BTC bear call BTC_USDC-27NOV26-69000-C / "
            "BTC_USDC-27NOV26-71000-C (short delta +0.220): credit 480 per BTC, 441.06 after "
            "fees; max loss 1,995.44 USDC",
        ),
        # Every price 20% lower: no spread in the band reaches the minimum credit.
        (
            lambda o: (
                replace(o, bid=o.bid * 0.8, ask=o.ask * 0.8)
                if o.expiry_ms == E1 and o.option_type == "call"
                else o
            ),
            "no trade: no bear call spread passes every check: 4 short legs in the delta band, "
            "0 pass every check; credit after fees under 20% of the width: 4",
        ),
    ],
)
def test_the_minimum_credit_decides_between_candidates(database_url: str, change, outcome) -> None:
    market(database_url)
    minute(database_url, T9, [change(o) for o in base_chain()])
    notes = paper(database_url).step(T9 + 30_000)
    assert notes[0] == f"decision for 2026-11-15: {outcome}"
    action, reason = one(database_url, "SELECT action, reason FROM rough_decisions")
    if outcome.startswith("no trade"):
        assert (action, reason) == ("no trade", outcome.removeprefix("no trade: "))
    else:
        assert action == "entered"
        assert outcome.endswith(reason)


@pytest.mark.db
def test_no_entry_when_no_btc_usdc_expiry_is_7_to_30_days_away(database_url: str) -> None:
    t = utc_ms(2026, 11, 21, 9)  # 27 Nov is 5.96 days away, 25 Dec 33.96
    market(database_url, end=date(2026, 11, 21))
    minute(database_url, t, base_chain())
    notes = paper(database_url).step(t + 30_000)
    assert notes == ["decision for 2026-11-21: no trade: no BTC_USDC expiry is 7 to 30 days away"]


@pytest.mark.db
def test_flagged_minutes_are_skipped(database_url: str) -> None:
    url = database_url
    market(url)
    minute(url, T9, base_chain(), indexes=("btc_usd",))  # btc_usdc index missing
    minute(url, T9 + M, base_chain())
    minute(url, T9 + 2 * M, base_chain())
    with psycopg.connect(url) as conn:  # the hourly checks marked 09:01 to skip
        conn.execute(
            "INSERT INTO data_quality (hour_ts, check_name, scope, subject, start_ts, end_ts, "
            "minutes, skip, detail, detected_ms) VALUES (%s, 'partial_snapshot', 'global', '', "
            "%s, %s, 1, true, 'test', %s)",
            (ms_to_datetime(T9), ms_to_datetime(T9 + M), ms_to_datetime(T9 + 2 * M), T9 + 2 * M),
        )
    p = paper(url)
    assert p.step(T9 + 30_000) == ["skipped 2026-11-15 09:00 UTC: index btc_usdc missing"]
    assert p.step(T9 + M + 30_000) == [
        "skipped 2026-11-15 09:01 UTC: marked to skip by the data checks"
    ]
    assert p.step(T9 + 2 * M + 30_000)[0].startswith("decision for 2026-11-15: entered")
    assert one(url, "SELECT ts_ms FROM rough_decisions") == T9 + 2 * M
    assert one(url, "SELECT minutes, skipped FROM rough_runs") == (1, 2)


@pytest.mark.db
def test_a_partial_snapshot_is_skipped(database_url: str) -> None:
    market(database_url)
    minute(database_url, T9, base_chain()[:5])  # 5 of 10 listed options
    notes = paper(database_url).step(T9 + 30_000)
    assert notes == ["skipped 2026-11-15 09:00 UTC: partial snapshot: 5 of 10 listed options"]


def test_the_paper_service_refuses_live_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_TRADING", "true")
    with pytest.raises(ValidationError, match="LIVE_TRADING=true is refused"):
        rough_paper.main(["--once"])


ROUGH_MODULES = (
    "volatility/rough.py",
    "strategy/rough.py",
    "risk/rough.py",
    "ledger/rough.py",
    "simulator/rough_fees.py",
    "simulator/rough_store.py",
    "simulator/rough_paper.py",
)
FORBIDDEN = ("optlab.market_data.deribit", "optlab.execution", "websockets", "urllib", "http")


@pytest.mark.parametrize("path", ROUGH_MODULES)
def test_rough_code_never_imports_an_exchange_client_or_execution(path: str) -> None:
    """The rough rule and paper service send nothing anywhere: no client, no order routing."""
    tree = ast.parse((REPO_ROOT / "src" / "optlab" / path).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [n for n in names if n.startswith(FORBIDDEN)]


@pytest.mark.db
def test_a_paper_step_opens_no_network_connection(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every Python socket connection refused, a full step (decision and entry) works:
    the only connection is the database's, which libpq opened before."""
    market(database_url)
    minute(database_url, T9, base_chain())
    p = paper(database_url)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the paper service tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert p.step(T9 + 30_000)[0].startswith("decision for 2026-11-15: entered")


def test_dates_in_the_history_helper_are_consecutive() -> None:
    days = [d for d, _ in history(85_000, 0.99)]
    assert all(b - a == timedelta(days=1) for a, b in pairwise(days))


@pytest.mark.db
def test_the_status_page_shows_the_paper_trades(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamlit.testing.v1 import AppTest

    url = database_url
    market(url)
    minute(url, T9, base_chain())
    p = paper(url)
    p.step(T9 + 30_000)
    monkeypatch.setenv("DATABASE_URL", url)
    import streamlit as st

    st.cache_resource.clear()  # the page caches DATABASE_URL; each test has its own database
    app = AppTest.from_file(str(REPO_ROOT / "apps" / "dashboard" / "status.py"), default_timeout=30)
    app.run()
    assert not app.exception, app.exception
    assert [t.label for t in app.tabs] == [
        "Recorder",
        "Paper trading (ROUGH)",
        "Volatility surface",
    ]
    assert any("ROUGH end-to-end version" in w.value for w in app.warning)
    headers = [h.value for h in app.subheader]
    for header in (
        "Equity (rough), USDC",
        "Daily decisions",
        "Open spreads",
        "Trades (paper fills)",
    ):
        assert header in headers
    tables = {tuple(df.value.columns): df.value for df in app.dataframe}
    decisions = next(v for k, v in tables.items() if "reason" in k and "IV30" in k)
    assert list(decisions["action"]) == ["entered"]
    trades = next(v for k, v in tables.items() if "cash change" in k)
    assert sorted(trades["side"]) == ["buy", "sell"]
    assert set(trades["instrument"]) == {"BTC_USDC-27NOV26-70000-C", "BTC_USDC-27NOV26-72000-C"}
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Open spreads"] == "1"
