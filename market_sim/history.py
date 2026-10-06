"""Immutable single-position snapshots; floats round-trip and timestamps stay integers."""

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, fields
from pathlib import Path

from .models import Fill, LimitOrder, MarketState, OrderStatus, Portfolio, Side, as_float


ATTRIBUTION_COLUMNS = ('execution_pnl', 'inventory_pnl', 'fees_funding_pnl')


@dataclass
class PnlAttribution:
    """Causal cumulative execution edge and mark changes on the previous position."""

    execution_pnl: float = 0.0
    inventory_pnl: float = 0.0
    position_size: float = 0.0
    instrument_price: float | None = None

    def update(
        self, position_size: float, mark: float | None,
        *, signed_fill_quantity: float = 0.0, fill_price: float | None = None,
    ) -> None:
        # Mark the old position before attributing any new fill at this mark.
        if mark is not None and self.instrument_price is not None:
            self.inventory_pnl += self.position_size * (mark - self.instrument_price)
        elif self.position_size != 0 and mark != self.instrument_price:
            raise ValueError("Inventory attribution requires known previous and current marks")
        if signed_fill_quantity != 0:
            if mark is None or fill_price is None:
                raise ValueError("Execution attribution requires a known mark and fill price")
            self.execution_pnl += (mark - fill_price) * signed_fill_quantity
        self.position_size, self.instrument_price = position_size, mark


def _add_attribution_columns(rows: Iterable[dict[str, str]]) -> Iterator[dict[str, str]]:
    """Reconstruct older CSVs at full event resolution before any plot sampling."""
    attribution = PnlAttribution()
    for row in rows:
        position = as_float(row['position_size'])
        mark = as_float(row['instrument_price']) if row['instrument_price'] else None
        signed_quantity, price = 0.0, None
        if row['event'] == 'fill':
            if not row['fill_quantity'] or not row['fill_price'] or position == attribution.position_size:
                raise ValueError("Fill attribution requires quantity, price and a signed position change")
            quantity = as_float(row['fill_quantity'])
            signed_quantity = quantity if position > attribution.position_size else -quantity
            price = as_float(row['fill_price'])
        attribution.update(position, mark, signed_fill_quantity=signed_quantity, fill_price=price)
        row['execution_pnl'] = str(attribution.execution_pnl)
        row['inventory_pnl'] = str(attribution.inventory_pnl)
        row['fees_funding_pnl'] = str(as_float(row['funding_pnl']) - as_float(row['trading_fees']))
        yield row


@dataclass(frozen=True)
class PositionSnapshot:
    timestamp_ns: int | None
    event: str
    position_size: float
    instrument_price: float | None
    average_entry_price: float
    cash: float
    position_balance: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    trading_fees: float
    funding_pnl: float
    net_realized_pnl: float
    total_pnl: float
    funding_rate: float | None
    fee: float = 0.0
    funding_payment: float = 0.0
    order_id: int | None = None
    fill_price: float | None = None
    fill_quantity: float | None = None
    bid_price_1: float | None = None
    ask_price_1: float | None = None
    execution_pnl: float = 0.0
    inventory_pnl: float = 0.0
    fees_funding_pnl: float = 0.0


@dataclass(frozen=True)
class OrderEvent:
    """Order state after acceptance, rejection, cancellation or an individual fill."""

    sequence: int
    timestamp_ns: int
    event: str  # place, reject, cancel, fill; status distinguishes partial/full fills
    order_id: int
    side: Side
    price: float
    original_quantity: float
    remaining_quantity: float
    placement_time_ns: int
    status: OrderStatus
    instrument_price: float | None
    fill_price: float | None = None
    fill_quantity: float = 0.0
    bid_price_1: float | None = None
    ask_price_1: float | None = None
    activation_timestamp_ns: int | None = None
    trade_timestamp_ns: int | None = None
    trade_price: float | None = None
    queue_ahead: float | None = None
    queue_ahead_before: float | None = None
    queue_ahead_after: float | None = None
    fill_reason: str | None = None
    rejection_reason: str | None = None


def order_snapshot(
    state: MarketState, order: LimitOrder, event: str, sequence: int,
    fill: Fill | None = None,
) -> OrderEvent:
    if state.timestamp_ns is None:
        raise ValueError("Order history requires a current timestamp")
    return OrderEvent(
        sequence, state.timestamp_ns, event, order.order_id, order.side,
        as_float(order.price), as_float(order.original_quantity), order.remaining_quantity,
        order.placement_time_ns, order.status, state.mark_price,
        as_float(fill.price) if fill else None,
        as_float(fill.quantity) if fill else 0.0,
        as_float(state.book.bids[0][0]) if state.book and state.book.bids else None,
        as_float(state.book.asks[0][0]) if state.book and state.book.asks else None,
        activation_timestamp_ns=(order.activation_timestamp_ns if order.activation_timestamp_ns is not None
                                 else state.timestamp_ns if event == 'reject'
                                 and state.timestamp_ns == order.placement_time_ns else None),
        trade_timestamp_ns=fill.timestamp_ns if fill else None,
        trade_price=fill.trade_price if fill else None,
        queue_ahead=order.queue_ahead,
        queue_ahead_before=fill.queue_ahead_before if fill else None,
        queue_ahead_after=fill.queue_ahead_after if fill else None,
        fill_reason=fill.fill_reason if fill else None,
        rejection_reason=order.rejection_reason,
    )


def save_order_history(path: str | Path, history: Iterable[OrderEvent]) -> None:
    names = [field.name for field in fields(OrderEvent)]
    with Path(path).open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(names)
        for record in history:
            writer.writerow([
                value.value if isinstance(value, (Side, OrderStatus)) else value
                for value in (getattr(record, name) for name in names)
            ])


def load_order_history(path: str | Path) -> Iterator[OrderEvent]:
    names = [field.name for field in fields(OrderEvent)]
    integer_fields = {"sequence", "timestamp_ns", "order_id", "placement_time_ns",
                      "activation_timestamp_ns", "trade_timestamp_ns"}
    optional_fields = {'bid_price_1', 'ask_price_1', 'activation_timestamp_ns', 'trade_timestamp_ns',
                       'trade_price', 'queue_ahead', 'queue_ahead_before', 'queue_ahead_after',
                       'fill_reason', 'rejection_reason'}
    previous: OrderEvent | None = None
    with Path(path).open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        missing = set(names) - optional_fields - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Order history CSV is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            values = {}
            for name in names:
                value = row.get(name, "")
                if name in integer_fields:
                    values[name] = int(value) if value else None
                elif name == "side":
                    values[name] = Side(value)
                elif name == "status":
                    values[name] = OrderStatus(value)
                elif name == "event":
                    values[name] = value
                elif name in ('fill_reason', 'rejection_reason'):
                    values[name] = value or None
                else:
                    values[name] = as_float(value) if value else None
            record = OrderEvent(**values)
            if previous and (record.sequence <= previous.sequence or record.timestamp_ns < previous.timestamp_ns):
                raise ValueError("Order history must have increasing sequences and chronological timestamps")
            previous = record
            yield record


def snapshot(
    state: MarketState, portfolio: Portfolio, event: str,
    fill: Fill | None = None, funding_payment: float = 0.0,
    attribution: PnlAttribution | None = None,
) -> PositionSnapshot:
    mark = state.mark_price
    attribution = attribution if attribution is not None else PnlAttribution()
    signed_quantity = (fill.quantity if fill.side == Side.BUY else -fill.quantity) if fill else 0.0
    attribution.update(portfolio.inventory, mark, signed_fill_quantity=signed_quantity,
                       fill_price=fill.price if fill else None)
    return PositionSnapshot(
        timestamp_ns=state.timestamp_ns, event=event,
        position_size=portfolio.inventory, instrument_price=mark,
        average_entry_price=portfolio.average_entry_price,
        cash=portfolio.cash, position_balance=portfolio.position_balance(mark),
        equity=portfolio.equity(mark), realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=portfolio.unrealized_pnl(mark), trading_fees=portfolio.trading_fees,
        funding_pnl=portfolio.funding_pnl, net_realized_pnl=portfolio.net_realized_pnl,
        total_pnl=portfolio.pnl(mark),
        funding_rate=as_float(state.funding.funding_rate) if state.funding else None,
        fee=fill.quantity * fill.price * portfolio.maker_fee_rate if fill else 0.0,
        funding_payment=funding_payment,
        order_id=fill.order_id if fill else None,
        fill_price=fill.price if fill else None,
        fill_quantity=fill.quantity if fill else None,
        bid_price_1=as_float(state.book.bids[0][0]) if state.book and state.book.bids else None,
        ask_price_1=as_float(state.book.asks[0][0]) if state.book and state.book.asks else None,
        execution_pnl=attribution.execution_pnl, inventory_pnl=attribution.inventory_pnl,
        fees_funding_pnl=portfolio.funding_pnl - portfolio.trading_fees,
    )


def save_history(path: str | Path, history: Iterable[PositionSnapshot]) -> None:
    """Write round-trippable float strings and integer ns; unknown values are blank."""
    names = [field.name for field in fields(PositionSnapshot)]
    with Path(path).open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(names)
        for record in history:
            writer.writerow([getattr(record, name) for name in names])


def load_history(path: str | Path) -> Iterator[PositionSnapshot]:
    """Read numeric floats and integer timestamps, including earlier decimal-string CSVs."""
    names = [field.name for field in fields(PositionSnapshot)]
    with Path(path).open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        missing = set(names) - {"bid_price_1", "ask_price_1", *ATTRIBUTION_COLUMNS} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"History CSV is missing columns: {', '.join(sorted(missing))}")
        rows = reader if set(ATTRIBUTION_COLUMNS) <= set(reader.fieldnames or []) else _add_attribution_columns(reader)
        for row in rows:
            values = {}
            for name in names:
                value = row.get(name, "")
                if name == "event":
                    values[name] = value
                elif value == "":
                    values[name] = None
                elif name in ("timestamp_ns", "order_id"):
                    values[name] = int(value)
                else:
                    values[name] = as_float(value)
            yield PositionSnapshot(**values)
