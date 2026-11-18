"""The backtest event loop over point-in-time snapshots (Phase 4).

For each snapshot minute t in [start, end), in this order:

1. read the market through ``MarketDataView(source, as_of=t)`` (nothing after t);
2. settle options that expired, once Deribit's delivery price is known at t (two-step booking
   through the expiry's future, one delivery fee per in-the-money option);
3. book perpetual funding for the minutes since the last snapshot, at the rate seen then;
4. update the risk monitors (peak equity, the day's start, maintenance margin);
5. unless the data checks marked the minute to skip: ask the strategy for tickets, and for
   each one work out the fill, pass it through the risk gateway, and book it if approved;
6. value the books at Deribit's mark (and at the bid/ask alongside), check the reconciliation
   (change in equity = option P&L + hedge P&L - fees + funding, exactly), and record equity.

Every minute is processed, including ones after a gap, so exits are never skipped (rough
shortcut 14). An open position whose option has no mark at t is valued at its last mark and
counted as stale (shortcut 21). A strategy's random choices come from ``config.seed``.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from optlab.domain.checks import need
from optlab.domain.clock import ms_to_datetime
from optlab.domain.money import quantize
from optlab.domain.orders import Ticket
from optlab.ledger.books import Books, Reconciliation, intrinsic
from optlab.ledger.journal import Journal
from optlab.market_data.view import MarketDataSource, MarketDataView, snapshot_times
from optlab.risk.gateway import RiskContext, RiskEvent, RiskGateway, Valuation
from optlab.risk.limits import RiskConfig, load_risk_config
from optlab.simulator.config import BacktestConfig
from optlab.simulator.fees import FeeSchedule, funding_payment, funding_rate, load_fees
from optlab.simulator.fills import TicketFill, fill_ticket
from optlab.simulator.groups import Group
from optlab.simulator.market import InstrumentCache, InstrumentInfo, MarketState, Quote
from optlab.simulator.market import load_market_state as _load_state
from optlab.simulator.records import (
    EquityRecord,
    FillRecord,
    GroupRecord,
    OrderRecord,
    PositionRecord,
    result_hash,
)
from optlab.strategy.base import GroupView, PortfolioView, Strategy, make_strategy

ZERO = Decimal(0)


class ReconciliationError(AssertionError):
    """Equity moved by something the P&L accounts don't explain."""


@dataclass
class BacktestResult:
    config: BacktestConfig
    minutes: int
    orders: list[OrderRecord]
    fills: list[FillRecord]
    positions: list[PositionRecord]
    equity: list[EquityRecord]
    risk_events: list[RiskEvent]
    groups: list[GroupRecord]
    journal: Journal
    capital: Decimal
    result_hash: str = ""

    @property
    def final(self) -> EquityRecord | None:
        return self.equity[-1] if self.equity else None

    def sections(self) -> dict[str, list[object]]:
        return {
            "orders": list(self.orders),
            "fills": list(self.fills),
            "positions": list(self.positions),
            "equity": list(self.equity),
            "risk_events": list(self.risk_events),
            "groups": list(self.groups),
        }


@dataclass
class _Measure:
    marks: dict[str, Decimal]
    stale: int
    rec: Reconciliation
    ctx: RiskContext | None


@dataclass
class Backtest:
    config: BacktestConfig
    source: MarketDataSource
    strategy: Strategy | None = None
    fees: FeeSchedule | None = None
    risk: RiskConfig | None = None
    config_dir: Path = Path("configs")
    # state
    books: Books = field(init=False)
    gateway: RiskGateway = field(init=False)
    groups: dict[str, Group] = field(init=False, default_factory=dict)
    infos: dict[str, InstrumentInfo] = field(init=False, default_factory=dict)
    last: dict[str, Quote] = field(init=False, default_factory=dict)
    fill_marks: dict[str, Decimal] = field(init=False, default_factory=dict)
    funding_at: dict[str, tuple[int, Decimal, Decimal]] = field(init=False, default_factory=dict)
    orders: list[OrderRecord] = field(init=False, default_factory=list)
    fills: list[FillRecord] = field(init=False, default_factory=list)
    positions: list[PositionRecord] = field(init=False, default_factory=list)
    equity: list[EquityRecord] = field(init=False, default_factory=list)
    events: list[RiskEvent] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        fees = self.fees if self.fees is not None else load_fees(self.config_dir)
        self._fees = fees.scaled(self.config.fee_multiplier)
        risk = self.risk or load_risk_config(self.config_dir, self.config.risk)
        s = self.config.strategy
        self._strategy = self.strategy or make_strategy(s.name, s.params, self.config.seed)
        self.books = Books(risk.starting_equity, risk.currency, self.config.start_ms)
        self.gateway = RiskGateway(risk, self._fees)
        self._next_group = 0
        self._next_fill = 0

    # ------------------------------------------------------------------ run

    def run(self) -> BacktestResult:
        cfg = self.config
        cache = InstrumentCache()
        skips = sorted(self.source.skip_periods(cfg.start_ms, cfg.end_ms))
        minutes = 0
        for t in snapshot_times(self.source, cfg.start_ms, cfg.end_ms - 1, skip_flagged=False):
            flagged = any(s <= t < e for s, e in skips)
            state = _load_state(MarketDataView(self.source, as_of=t), cache, flagged=flagged)
            self._step(state)
            minutes += 1
        result = BacktestResult(
            config=cfg,
            minutes=minutes,
            orders=self.orders,
            fills=self.fills,
            positions=self.positions,
            equity=self.equity,
            risk_events=self.events,
            groups=[self._group_record(g) for g in self.groups.values()],
            journal=self.books.journal,
            capital=self.books.capital,
        )
        result.result_hash = result_hash(result.sections())
        return result

    def _step(self, state: MarketState) -> None:
        self._remember(state)
        self._settle(state)
        self._fund(state)
        m = self._measure(state)
        if m.ctx is not None:
            balance = m.rec.cash + m.ctx.hedge_unrealised  # Deribit's margin balance
            exp = self.gateway.exposure(m.ctx.positions, m.ctx, m.rec.cash)
            self.events.extend(
                self.gateway.on_minute(state.ts_ms, m.rec.equity, balance, exp.margin.maintenance)
            )
        if not (state.flagged and self.config.skip_flagged_minutes):
            tickets = self._strategy.on_snapshot(state, self._portfolio(state, m))
            for n, ticket in enumerate(tickets):
                self._process(ticket, state, f"{state.ts_ms}-{n}")
        self._record(state)

    # ------------------------------------------------------------------ market data

    def _remember(self, state: MarketState) -> None:
        for name in list(self.books.open_positions()) + list(state.underlyings):
            q = state.quote(name)
            if q is not None and q.mark is not None:
                self.last[name] = q
        for name, info in state.instruments.items():
            if name in self.books.positions:
                self.infos[name] = info

    def _mark(self, name: str, state: MarketState) -> tuple[Decimal, bool]:
        q = state.quote(name)
        if q is not None and q.mark is not None:
            return q.mark, False
        last = self.last.get(name)
        if last is not None and last.mark is not None:
            return last.mark, True
        return self.fill_marks[name], True

    def _valuation(self, name: str, state: MarketState) -> Valuation:
        info = state.instruments.get(name) or self.infos[name]
        mark, _ = self._mark(name, state)
        q = state.quote(name)
        if q is None or q.mark is None:
            q = self.last.get(name)
        if info.kind == "perpetual":
            return Valuation(info, mark, 1.0, 0.0)
        return Valuation(
            info, mark,
            None if q is None else q.delta,
            None if q is None else q.vega,
            None if q is None else q.underlying_price,
        )  # fmt: skip

    def _measure(self, state: MarketState, extra: Iterable[str] = ()) -> _Measure:
        open_pos = self.books.open_positions()
        marks: dict[str, Decimal] = {}
        stale = 0
        for name in open_pos:
            marks[name], was_stale = self._mark(name, state)
            stale += was_stale
        rec = self.books.reconcile(marks)
        ctx = None
        if state.index is not None:
            names = set(open_pos) | set(extra)
            ctx = RiskContext(
                ts_ms=state.ts_ms,
                index=state.index,
                equity=rec.equity,
                cash=rec.cash,
                hedge_unrealised=self.books.hedge_unrealised(marks),
                positions={n: p.qty for n, p in open_pos.items()},
                groups=[g for g in self.groups.values() if g.is_open],
                valuations={n: self._valuation(n, state) for n in names},
            )
        return _Measure(marks, stale, rec, ctx)

    # ------------------------------------------------------------------ settlement, funding

    def _settle(self, state: MarketState) -> None:
        for name, pos in list(self.books.open_positions().items()):
            if pos.kind != "option":
                continue
            info = self.infos[name]
            expiry = need(info.expiry_ms, f"{name} expiry")
            strike = need(info.strike, f"{name} strike")
            option_type = need(info.option_type, f"{name} option type")
            if expiry > state.ts_ms:
                continue
            price = state.delivery_price(expiry)
            if price is None:
                continue  # Deribit publishes it at 08:01; valued at the last mark until then
            future = state.expiry_future(expiry) or f"future expiring {info.expiry_date}"
            value = intrinsic(option_type, strike, price)
            fee = self._fees.option_delivery_fee(value, price, abs(pos.qty), expiry)
            holders = [g for g in self.groups.values() if g.legs.get(name, ZERO) != 0]
            if sum((g.legs[name] for g in holders), ZERO) != pos.qty:
                raise ReconciliationError(f"{name}: groups and books disagree on the position")
            self.books.settle_option(state.ts_ms, name, option_type, strike, price, future, fee)
            total = sum((abs(g.legs[name]) for g in holders), ZERO)
            left = fee
            for i, g in enumerate(holders):
                qty = g.legs[name]
                share = left if i == len(holders) - 1 else quantize(fee * abs(qty) / total)
                left -= share
                g.settle(name, value)
                g.fees += share
                self._fill_record(
                    None, g.group_id, state.ts_ms, name, "settlement",
                    "sell" if qty > 0 else "buy", abs(qty), value, share, state.index, None,
                    None, price,
                )  # fmt: skip
                self.positions.append(PositionRecord(state.ts_ms, g.group_id, name, ZERO, ZERO))
                self._maybe_close(g, state.ts_ms, "settled at expiry")

    def _fund(self, state: MarketState) -> None:
        rules = self._fees.funding
        for name, (ts, rate, index) in list(self.funding_at.items()):
            pos = self.books.positions.get(name)
            if pos is not None and pos.qty != 0 and state.ts_ms > ts:
                paid = funding_payment(rate, pos.qty, index, state.ts_ms - ts, rules)
                self.books.funding(state.ts_ms, name, paid)
        for name, q in state.underlyings.items():
            if name in rules.cap and q.mark is not None:
                now_index = q.index_price if q.index_price is not None else state.index
                if now_index is not None:
                    rate_now = funding_rate(q.mark, now_index, rules, name)
                    self.funding_at[name] = (state.ts_ms, rate_now, now_index)

    # ------------------------------------------------------------------ tickets

    def _process(self, ticket: Ticket, state: MarketState, ticket_id: str) -> None:
        fill = fill_ticket(ticket, state, self._fees)
        gid = (
            f"g{self._next_group + 1}"
            if ticket.intent == "open"
            else need(ticket.group_id, "group")
        )
        if fill.filled == 0:
            self._order_records(ticket, ticket_id, gid, state.ts_ms, fill, "unfilled", fill.reason)
            return
        m = self._measure(state, extra=[leg.instrument for leg in fill.legs])
        if m.ctx is None:
            self._order_records(ticket, ticket_id, gid, state.ts_ms, fill, "unfilled",
                                "no index at this minute")  # fmt: skip
            return
        events = self.gateway.check(ticket, fill, m.ctx, ticket_id=ticket_id)
        if events:
            self.events.extend(
                RiskEvent(e.ts_ms, e.rule, e.action, e.detail, e.ticket_id, gid, e.value, e.limit)
                for e in events
            )
            detail = "; ".join(f"{e.rule}: {e.detail}" for e in events)
            self._order_records(ticket, ticket_id, gid, state.ts_ms, fill, "rejected", detail)
            return
        if ticket.intent == "open":
            self._next_group += 1
            self.groups[gid] = Group(gid, state.ts_ms, thesis=ticket.thesis, reason=ticket.reason)
        group = self.groups[gid]
        for leg in fill.legs:
            info = state.instruments[leg.instrument]
            self.infos[leg.instrument] = info
            # The value of last resort, if the instrument is never marked again.
            self.fill_marks[leg.instrument] = leg.mark if leg.mark is not None else leg.price
            if leg.kind == "option":
                self.books.option_fill(state.ts_ms, leg.instrument, leg.direction, leg.amount,
                                       leg.price, leg.fee, ref=ticket_id)  # fmt: skip
            else:
                self.books.perpetual_fill(state.ts_ms, leg.instrument, leg.direction, leg.amount,
                                          leg.price, leg.fee, ref=ticket_id)  # fmt: skip
            group.apply(leg.instrument, leg.signed, leg.price)
            group.fees += leg.fee
            self._fill_record(ticket_id, gid, state.ts_ms, leg.instrument, "trade",
                              leg.direction, leg.amount, leg.price, leg.fee, leg.index_price,
                              leg.mid, leg.mark)  # fmt: skip
            self.positions.append(
                PositionRecord(state.ts_ms, gid, leg.instrument, group.legs[leg.instrument],
                               group.entry[leg.instrument])
            )  # fmt: skip
        status = "filled" if fill.filled == fill.requested else "partial"
        self._order_records(ticket, ticket_id, gid, state.ts_ms, fill, status, fill.reason)
        self._maybe_close(group, state.ts_ms, ticket.reason or "closed")

    def _maybe_close(self, group: Group, ts_ms: int, reason: str) -> None:
        if not group.is_open and group.closed_ms is None:
            group.closed_ms = ts_ms
            group.exit_reason = reason

    # ------------------------------------------------------------------ records

    def _portfolio(self, state: MarketState, m: _Measure) -> PortfolioView:
        s = self.gateway.state
        day = ms_to_datetime(state.ts_ms).date()
        return PortfolioView(
            ts_ms=state.ts_ms,
            cash=m.rec.cash,
            equity=m.rec.equity,
            positions={n: p.qty for n, p in self.books.open_positions().items()},
            groups=[
                GroupView(g.group_id, g.opened_ms, g.open_legs(), dict(g.entry), g.thesis)
                for g in self.groups.values()
                if g.is_open
            ],
            can_open=not (s.killed or s.halted_day == day or s.margin_short),
        )

    def _record(self, state: MarketState) -> None:
        m = self._measure(state)
        rec = m.rec
        capital = self.books.capital
        explained = rec.option_pnl + rec.hedge_pnl - rec.fees + rec.funding
        if rec.equity - capital != explained:
            raise ReconciliationError(
                f"{ms_to_datetime(state.ts_ms):%Y-%m-%d %H:%M}: equity change "
                f"{rec.equity - capital} but the P&L parts sum to {explained}"
            )
        if any(v != 0 for v in self.books.journal.trial_balance().values()):
            raise ReconciliationError("the journal does not balance")
        liquidation = rec.cash
        option_value = ZERO
        for name, pos in self.books.open_positions().items():
            q = state.quote(name)
            mark = m.marks[name]
            side = None
            if q is not None:
                side = q.bid if pos.qty > 0 else q.ask
            price = side if side is not None else mark
            if pos.kind == "option":
                option_value += pos.qty * mark
                liquidation += pos.qty * price
            else:
                liquidation += pos.qty * price - pos.entry_value
        if m.ctx is not None:
            exp = self.gateway.exposure(m.ctx.positions, m.ctx, rec.cash)
            losses = self.gateway.group_losses(
                m.ctx.groups, {n: v.info for n, v in m.ctx.valuations.items()}, m.ctx.index
            )
            max_loss = (
                None
                if any(x is None for x in losses)
                else sum((x for x in losses if x is not None), ZERO)
            )
            im, mm, delta, vega = exp.margin.initial, exp.margin.maintenance, exp.delta, exp.vega
        else:
            prev = self.equity[-1] if self.equity else None
            max_loss = prev.max_loss if prev else ZERO
            im = prev.initial_margin if prev else ZERO
            mm = prev.maintenance_margin if prev else ZERO
            delta = prev.delta if prev else ZERO
            vega = prev.vega if prev else ZERO
        self.equity.append(
            EquityRecord(
                ts_ms=state.ts_ms,
                equity=rec.equity,
                liquidation_equity=liquidation,
                cash=rec.cash,
                option_value=option_value,
                hedge_unrealised=self.books.hedge_unrealised(m.marks),
                option_pnl=rec.option_pnl,
                hedge_pnl=rec.hedge_pnl,
                fees=rec.fees,
                funding=rec.funding,
                open_groups=sum(1 for g in self.groups.values() if g.is_open),
                max_loss=max_loss,
                initial_margin=im,
                maintenance_margin=mm,
                delta=delta,
                vega=vega,
                stale_marks=m.stale,
            )
        )

    def _order_records(
        self,
        ticket: Ticket,
        ticket_id: str,
        gid: str | None,
        ts_ms: int,
        fill: TicketFill,
        status: str,
        detail: str,
    ) -> None:
        for i, o in enumerate(ticket.orders):
            self.orders.append(
                OrderRecord(ticket_id, gid, ts_ms, i, o.instrument, o.direction, ticket.intent,
                            o.amount, fill.filled if status in ("filled", "partial") else ZERO,
                            status, ticket.reason, detail)
            )  # fmt: skip

    def _fill_record(
        self,
        ticket_id: str | None,
        gid: str | None,
        ts_ms: int,
        instrument: str,
        kind: str,
        direction: str,
        amount: Decimal,
        price: Decimal,
        fee: Decimal,
        index: Decimal | None,
        mid: Decimal | None,
        mark: Decimal | None,
        delivery: Decimal | None = None,
    ) -> None:
        self._next_fill += 1
        self.fills.append(
            FillRecord(self._next_fill, ticket_id, gid, ts_ms, instrument, kind, direction,
                       amount, price, fee, index, mid, mark, delivery)
        )  # fmt: skip

    @staticmethod
    def _group_record(g: Group) -> GroupRecord:
        return GroupRecord(g.group_id, g.opened_ms, g.closed_ms, g.thesis, g.reason,
                           g.exit_reason, g.realised, g.fees)  # fmt: skip


def run_backtest(
    config: BacktestConfig,
    source: MarketDataSource,
    *,
    config_dir: Path = Path("configs"),
    strategy: Strategy | None = None,
) -> BacktestResult:
    return Backtest(config, source, strategy=strategy, config_dir=config_dir).run()


def equity_of(result: BacktestResult) -> Mapping[int, Decimal]:
    return {r.ts_ms: r.equity for r in result.equity}
