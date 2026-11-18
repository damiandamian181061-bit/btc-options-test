"""The risk gateway: every ticket passes here before anything is booked.

It checks the exact fill the simulator worked out (prices, amount, fees) against the limits of
``configs/risk/<name>.yaml`` (the brief's "Sizing and limits" table) and Deribit Standard Margin.
An opening ticket that breaches any limit is rejected, with one risk event per breached rule.
A closing ticket only has to reduce its own group; it is never blocked by a limit, so exits
always work.

Opening tickets are checked for, in order:

1. the kill switch (10% drawdown from peak equity: no new trades until Damian reviews);
2. the daily loss limit (3% loss within the UTC day, cut at 00:00: no new trades that day);
3. a maintenance-margin shortfall (no new trades while margin balance < maintenance margin);
4. the group's maximum loss (2% of equity) and the open groups' total (10% of equity);
5. open groups (5) and open groups per expiry (2);
6. portfolio delta (|delta x index| at most 20% of equity) and vega (0.5% of equity a vol point);
7. initial margin (at most the margin balance) and Deribit's limits (short options 1,000,000
   USDC, perpetual size).

A group's maximum loss is its worst P&L at expiry over every delivery price (it is piecewise
linear, so the worst is at a strike, at zero, or unbounded), less the entry value, plus the
fees it paid and the fees to close it at the current index (the taker rate on every leg, which
is above the delivery fee rate while the price is below twice the index). A group with a
perpetual, or a short option that outlives another of its legs, has no bounded maximum loss and
is refused while the loss limits are on. Equity is at Deribit's mark (Damian, 18 Nov 2026).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from optlab.domain.checks import need
from optlab.domain.clock import ms_to_datetime
from optlab.domain.orders import Ticket
from optlab.ledger.books import intrinsic
from optlab.risk.limits import RiskConfig
from optlab.risk.margin import Margin, OptionMarginInput, option_margin, perpetual_margin
from optlab.simulator.fees import FeeSchedule
from optlab.simulator.fills import TicketFill
from optlab.simulator.groups import Group
from optlab.simulator.market import InstrumentInfo

ZERO = Decimal(0)


@dataclass(frozen=True)
class RiskEvent:
    ts_ms: int
    rule: str
    action: str  # rejected, triggered or cleared
    detail: str
    ticket_id: str | None = None
    group_id: str | None = None
    value: Decimal | None = None
    limit: Decimal | None = None


@dataclass(frozen=True)
class Valuation:
    """What the gateway needs to know about one instrument at t (carried if not quoted)."""

    info: InstrumentInfo
    mark: Decimal
    delta: float | None = None  # per BTC; 1 for a perpetual
    vega: float | None = None  # USD per vol point, per BTC
    underlying_mark: Decimal | None = None  # an option's forward (its expiry future's mark)


@dataclass(frozen=True)
class RiskContext:
    ts_ms: int
    index: Decimal
    equity: Decimal  # at mark
    cash: Decimal
    hedge_unrealised: Decimal
    positions: Mapping[str, Decimal]  # signed BTC per instrument, from the books
    groups: Sequence[Group]  # open groups
    valuations: Mapping[str, Valuation]  # every instrument held or in the ticket


@dataclass
class RiskState:
    peak_equity: Decimal | None = None
    day: date | None = None
    day_start_equity: Decimal | None = None
    halted_day: date | None = None
    killed: bool = False
    margin_short: bool = False


@dataclass(frozen=True)
class Exposure:
    """Portfolio totals after a ticket (or now)."""

    delta: Decimal  # BTC
    vega: Decimal  # USD per vol point
    margin: Margin
    cash: Decimal


@dataclass
class RiskGateway:
    config: RiskConfig
    fees: FeeSchedule
    state: RiskState = field(default_factory=RiskState)

    # ------------------------------------------------------------------ every minute

    def on_minute(
        self, ts_ms: int, equity: Decimal, margin_balance: Decimal, maintenance: Decimal
    ) -> list[RiskEvent]:
        """Update the monitors (peak, daily loss, maintenance margin) before any decision."""
        s, c = self.state, self.config
        events: list[RiskEvent] = []
        day = ms_to_datetime(ts_ms).date()
        if s.day != day or s.day_start_equity is None:  # a UTC day's first minute: 00:00 cut
            s.day, s.day_start_equity = day, equity
        s.peak_equity = equity if s.peak_equity is None else max(s.peak_equity, equity)
        start = s.day_start_equity
        if c.daily_loss_limit is not None and s.halted_day != day:
            floor = start * (1 - c.daily_loss_limit)
            if equity <= floor:
                s.halted_day = day
                loss = (start - equity) / start
                events.append(
                    RiskEvent(ts_ms, "daily loss limit", "triggered",
                              f"equity {equity} is {loss:.2%} below the day's start "
                              f"{start}: no new trades until 00:00 UTC",
                              value=equity, limit=floor)
                )  # fmt: skip
        if c.kill_switch_drawdown is not None and not s.killed:
            floor = s.peak_equity * (1 - c.kill_switch_drawdown)
            if equity <= floor:
                s.killed = True
                events.append(
                    RiskEvent(ts_ms, "kill switch", "triggered",
                              f"equity {equity} is {c.kill_switch_drawdown:.0%} or more below its "
                              f"peak {s.peak_equity}: no new trades until reviewed",
                              value=equity, limit=floor)
                )  # fmt: skip
        short = maintenance > margin_balance
        if short != s.margin_short:
            s.margin_short = short
            events.append(
                RiskEvent(ts_ms, "maintenance margin", "triggered" if short else "cleared",
                          f"maintenance margin {maintenance} against margin balance "
                          f"{margin_balance}", value=maintenance, limit=margin_balance)
            )  # fmt: skip
        return events

    # ------------------------------------------------------------------ tickets

    def check(
        self, ticket: Ticket, fill: TicketFill, ctx: RiskContext, *, ticket_id: str
    ) -> list[RiskEvent]:
        """Every rule the proposed fill breaks (empty: approved)."""
        gid = ticket.group_id
        if ticket.intent == "close":
            return self._check_close(ticket, fill, ctx, ticket_id)

        s, c = self.state, self.config
        out: list[RiskEvent] = []

        def reject(rule: str, detail: str, value: Decimal | None = None,
                   limit: Decimal | None = None) -> None:  # fmt: skip
            out.append(RiskEvent(ctx.ts_ms, rule, "rejected", detail, ticket_id, gid, value, limit))

        day = ms_to_datetime(ctx.ts_ms).date()
        if s.killed:
            reject("kill switch", "the 10% drawdown kill switch is on: no new trades")
        if s.halted_day == day:
            reject("daily loss limit", "the daily loss limit was hit today: no new trades")
        if s.margin_short:
            reject("maintenance margin", "margin balance is below maintenance margin")

        legs = {leg.instrument: leg.signed for leg in fill.legs}
        # Cash changes hands for option premiums only; a perpetual's entry value is notional.
        premiums = {leg.instrument: leg.signed * leg.price for leg in fill.legs
                    if leg.kind == "option"}  # fmt: skip
        new = Group(
            "(new)", ctx.ts_ms, legs=dict(legs),
            entry={leg.instrument: leg.signed * leg.price for leg in fill.legs},
            fees=sum((x.fee for x in fill.legs), ZERO),
        )  # fmt: skip
        infos = {n: v.info for n, v in ctx.valuations.items()}
        new_loss = group_max_loss(new, infos, self._close_fee_per_btc(ctx.index))
        if c.max_loss_of_equity is not None:
            limit = c.max_loss_of_equity * ctx.equity
            if new_loss is None:
                reject("max loss per group", "the group's loss is not bounded", None, limit)
            elif new_loss > limit:
                reject("max loss per group", f"maximum loss {new_loss} above {limit}",
                       new_loss, limit)  # fmt: skip
        if c.max_total_loss_of_equity is not None:
            limit = c.max_total_loss_of_equity * ctx.equity
            losses = [group_max_loss(g, infos, self._close_fee_per_btc(ctx.index))
                      for g in ctx.groups]  # fmt: skip
            if new_loss is None or any(x is None for x in losses):
                reject("max total loss", "an open group's loss is not bounded", None, limit)
            else:
                total = new_loss + sum((x for x in losses if x is not None), ZERO)
                if total > limit:
                    reject("max total loss", f"total maximum loss {total} above {limit}",
                           total, limit)  # fmt: skip
        if c.max_open_groups is not None and len(ctx.groups) + 1 > c.max_open_groups:
            reject("open groups", f"{len(ctx.groups)} groups already open",
                   Decimal(len(ctx.groups) + 1), Decimal(c.max_open_groups))  # fmt: skip
        if c.max_open_per_expiry is not None:
            expiry = group_expiry(new, infos)
            same = sum(1 for g in ctx.groups if group_expiry(g, infos) == expiry)
            if expiry is not None and same + 1 > c.max_open_per_expiry:
                reject("open groups per expiry", f"{same} groups already open on this expiry",
                       Decimal(same + 1), Decimal(c.max_open_per_expiry))  # fmt: skip

        after = dict(ctx.positions)
        for name, q in legs.items():
            after[name] = after.get(name, ZERO) + q
        cash_after = ctx.cash - sum(premiums.values(), ZERO) - new.fees
        exp = self.exposure(after, ctx, cash_after)
        if c.max_delta_of_equity is not None:
            limit = c.max_delta_of_equity * ctx.equity
            value = abs(exp.delta * ctx.index)
            if value > limit:
                reject("portfolio delta", f"|delta x index| {value} above {limit}", value, limit)
        if c.max_vega_of_equity is not None:
            limit = c.max_vega_of_equity * ctx.equity
            if abs(exp.vega) > limit:
                reject("portfolio vega", f"|vega| {abs(exp.vega)} above {limit}",
                       abs(exp.vega), limit)  # fmt: skip
        m = c.margin
        balance = cash_after + ctx.hedge_unrealised
        if exp.margin.initial > balance:
            reject("initial margin", f"initial margin {exp.margin.initial} above the margin "
                   f"balance {balance}", exp.margin.initial, balance)  # fmt: skip
        if exp.margin.short_notional > m.max_short_option_notional:
            reject("short option limit", f"short options {exp.margin.short_notional} USDC",
                   exp.margin.short_notional, m.max_short_option_notional)  # fmt: skip
        for name, q in after.items():
            v = ctx.valuations.get(name)
            if v is not None and v.info.kind == "perpetual" and abs(q) > m.perpetual_max_size:
                reject("perpetual size", f"{name} {q} BTC is beyond the modelled margin tier",
                       abs(q), m.perpetual_max_size)  # fmt: skip
        return out

    def _check_close(
        self, ticket: Ticket, fill: TicketFill, ctx: RiskContext, ticket_id: str
    ) -> list[RiskEvent]:
        group = next((g for g in ctx.groups if g.group_id == ticket.group_id), None)
        problem = ""
        if group is None:
            problem = f"group {ticket.group_id} is not open"
        else:
            for leg in fill.legs:
                held = group.legs.get(leg.instrument, ZERO)
                if held == 0 or (held > 0) == (leg.signed > 0) or leg.amount > abs(held):
                    problem = f"{leg.instrument}: a close must reduce the group's own position"
                    break
        if not problem:
            return []
        return [RiskEvent(ctx.ts_ms, "close", "rejected", problem, ticket_id, ticket.group_id)]

    # ------------------------------------------------------------------ measures

    def exposure(
        self, positions: Mapping[str, Decimal], ctx: RiskContext, cash: Decimal
    ) -> Exposure:
        delta = vega = ZERO
        legs: list[OptionMarginInput] = []
        perp = Margin(ZERO, ZERO, ZERO)
        for name, q in positions.items():
            if q == 0:
                continue
            v = ctx.valuations[name]
            if v.info.kind == "perpetual":
                delta += q
                pm = perpetual_margin(q, v.mark, self.config.margin)
                perp = Margin(perp.initial + pm.initial, perp.maintenance + pm.maintenance, ZERO)
                continue
            delta += q * Decimal(repr(v.delta)) if v.delta is not None else ZERO
            vega += q * Decimal(repr(v.vega)) if v.vega is not None else ZERO
            legs.append(
                OptionMarginInput(
                    need(v.info.option_type, f"{name} option type"),
                    need(v.info.strike, f"{name} strike"), q, v.mark, ctx.index,
                    v.underlying_mark if v.underlying_mark is not None else ctx.index,
                )
            )  # fmt: skip
        om = option_margin(legs, self.config.margin)
        margin = Margin(om.initial + perp.initial, om.maintenance + perp.maintenance,
                        om.short_notional)  # fmt: skip
        return Exposure(delta, vega, margin, cash)

    def _close_fee_per_btc(self, index: Decimal) -> Decimal:
        return self.fees.trading["linear"].taker * index * self.fees.multiplier

    def group_losses(self, groups: Sequence[Group], infos: Mapping[str, InstrumentInfo],
                     index: Decimal) -> list[Decimal | None]:  # fmt: skip
        return [group_max_loss(g, infos, self._close_fee_per_btc(index)) for g in groups]


def group_expiry(group: Group, infos: Mapping[str, InstrumentInfo]) -> int | None:
    expiries = [infos[n].expiry_ms for n in group.open_legs() if n in infos]
    known = [e for e in expiries if e is not None]
    return min(known) if known else None


def group_max_loss(
    group: Group, infos: Mapping[str, InstrumentInfo], close_fee_per_btc: Decimal
) -> Decimal | None:
    """The most the group can lose from here (USDC), or None if it is not bounded."""
    legs = group.open_legs()
    if not legs:
        return ZERO
    # (option type, strike, expiry, signed BTC) per open leg
    opts: list[tuple[str, Decimal, int, Decimal]] = []
    for name, q in legs.items():
        info = infos.get(name)
        if info is None or info.kind != "option":
            return None
        opts.append((
            need(info.option_type, f"{name} option type"),
            need(info.strike, f"{name} strike"),
            need(info.expiry_ms, f"{name} expiry"),
            q,
        ))  # fmt: skip
    first = min(expiry for _, _, expiry, _ in opts)
    if any(q < 0 and expiry != first for _, _, expiry, q in opts):
        return None  # a short leg outlives another leg: intrinsic value is no bound
    if sum((q for kind, _, _, q in opts if kind == "call"), ZERO) < 0:
        return None  # net short calls: unbounded as the price rises
    entry = sum((group.entry.get(name, ZERO) for name in legs), ZERO)
    prices = {ZERO} | {strike for _, strike, _, _ in opts}
    worst = min(
        sum((q * intrinsic(kind, strike, p) for kind, strike, _, q in opts), ZERO) - entry
        for p in prices
    )
    close_fees = sum((abs(q) for *_, q in opts), ZERO) * close_fee_per_btc
    return max(ZERO, -worst) + close_fees + group.fees
