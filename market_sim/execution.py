"""Conservative snapshot-initialized FIFO queues, advanced only by trade prints."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .models import (
    BookSnapshot, Fill, LimitOrder, Number, OrderStatus, Portfolio, Side, Trade,
    as_float, quantities_close,
)


class ExecutionModel(Protocol):
    def match(self, trade: Trade, orders: Sequence[LimitOrder]) -> list[Fill]:
        """Advance queue estimates and return fills; engine applies quantity/cash changes."""
        ...


class TradeExecutionModel:
    """Share print volume across visible queue consumption and own fills.

    A seller trade fills buys at/above its price; a buyer trade fills sells
    at/below its price. Fill at the resting order's price, best price then FIFO.
    Orders must predate the trade strictly, including across equal-time events.
    Snapshots and marketable order placement alone never cause fills.

    Touches consume visible queue ahead first. Same-price own orders share a
    public-queue prefix: consumed external volume advances all their estimates
    once, while own fills spend volume separately in FIFO order. Larger queue
    estimates on later submissions represent additional liquidity ahead of them.
    Strict trade-through zeros that level's queue without spending print volume
    on external depth; own fills still share the single historical print budget.
    """

    def match(self, trade: Trade, orders: Sequence[LimitOrder]) -> list[Fill]:
        price, size = as_float(trade.price), as_float(trade.size)
        if price <= 0:
            raise ValueError("Trade price must be finite and positive")
        if size < 0:
            raise ValueError("Trade size must be finite and nonnegative")
        side = Side.SELL if trade.is_maker_ask else Side.BUY
        eligible = [
            order for order in orders
            if order.is_active and order.side == side
            and order.placement_time_ns < trade.timestamp_ns
            and (quantities_close(order.price, price)
                 or (order.price < price if side == Side.SELL else order.price > price))
        ]
        eligible.sort(key=lambda order: (
            order.price if side == Side.SELL else -order.price,
            order.placement_time_ns, order.order_id,
        ))
        remaining = size
        fills = []
        public_consumed = 0.0
        level_price = None
        for order in eligible:
            if level_price is None or not quantities_close(level_price, order.price):
                level_price, public_consumed = order.price, 0.0
            before = order.queue_ahead
            touch = quantities_close(order.price, price)
            if touch:
                ahead = max(0.0, before - public_consumed)
                consumed = min(remaining, ahead)
                public_consumed += consumed
                remaining = max(0.0, remaining - consumed)
                order.queue_ahead = max(0.0, before - public_consumed)
            elif size > 0:
                # Trade-through is evidence this better-priced level was swept,
                # even if higher-priority own orders exhaust our print budget.
                order.queue_ahead = 0.0
            if remaining <= 0 or order.queue_ahead > 0:
                continue
            quantity = min(remaining, order.remaining_quantity)
            fills.append(Fill(
                order.order_id, side, order.price, quantity, trade.timestamp_ns,
                activation_timestamp_ns=order.activation_timestamp_ns,
                trade_price=price, queue_ahead_before=before,
                queue_ahead_after=order.queue_ahead,
                fill_reason='same_price_queue_depletion' if touch else 'strict_trade_through',
            ))
            remaining -= quantity
        return fills


@dataclass
class ExecutionEngine:
    portfolio: Portfolio
    model: ExecutionModel = field(default_factory=TradeExecutionModel)
    orders: dict[int, LimitOrder] = field(default_factory=dict, init=False)
    # Keep the complete archive for ID lookup, but scan only live orders on trades.
    # Pending submissions stay indexed until accepted; pending cancels stay fillable.
    _live_orders: dict[int, LimitOrder] = field(default_factory=dict, init=False, repr=False)
    _next_order_id: int = field(default=1, init=False)

    def place(self, side: Side, price: Number, quantity: Number, timestamp_ns: int,
              *, activate: bool = True, book: BookSnapshot | None = None) -> LimitOrder:
        """Submit an order. Standalone callers without a book have an empty queue.

        Runtime always submits pending and activates against its observed book.
        """
        order = LimitOrder(self._next_order_id, side, price, quantity, timestamp_ns)
        self.orders[order.order_id] = order
        self._live_orders[order.order_id] = order
        self._next_order_id += 1
        order.status = OrderStatus.PENDING_NEW
        if activate:
            if book is None:
                order.status = OrderStatus.OPEN
                order.activation_timestamp_ns = timestamp_ns
            else:
                self.activate(order.order_id, book, timestamp_ns)
        return order

    def reject(self, order_id: int, reason: str) -> None:
        order = self.orders[order_id]
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        self._live_orders.pop(order_id, None)

    def activate(self, order_id: int, book: BookSnapshot | None, timestamp_ns: int) -> bool:
        """Check passivity and capture displayed same-level depth exactly once."""
        order = self.orders[order_id]
        if order.status != OrderStatus.PENDING_NEW:
            return False
        if timestamp_ns < order.placement_time_ns:
            raise ValueError('Cannot activate before the scheduled placement time')
        if book is not None and book.timestamp_ns > timestamp_ns:
            raise ValueError('Activation requires an already-observed book')
        if book is None or not book.bids or not book.asks:
            self.reject(order_id, 'missing_book')
            return False
        try:
            bid, ask = as_float(book.bids[0][0]), as_float(book.asks[0][0])
        except (ValueError, TypeError):
            self.reject(order_id, 'invalid_book')
            return False
        if bid <= 0 or ask <= bid:
            self.reject(order_id, 'invalid_book')
            return False
        if ((order.side == Side.BUY and (order.price >= ask or quantities_close(order.price, ask)))
                or (order.side == Side.SELL and (order.price <= bid or quantities_close(order.price, bid)))):
            self.reject(order_id, 'nonpassive_at_activation')
            return False
        levels = book.bids if order.side == Side.BUY else book.asks
        queue = 0.0
        for level in levels:
            try:
                level_price, level_quantity = level
                if quantities_close(as_float(level_price), order.price):
                    quantity = as_float(level_quantity)
                    if quantity > 0:
                        queue += quantity
            except (ValueError, TypeError):
                continue
        order.queue_ahead = queue
        order.activation_timestamp_ns = order.placement_time_ns = timestamp_ns
        order.status = OrderStatus.OPEN
        return True

    def reserved_quantity(self, side: Side, *, exclude_order_id: int | None = None) -> float:
        """Active/pending placements, including pending cancels; exclude dead orders."""
        return sum(order.remaining_quantity for order in self._live_orders.values()
                   if order.is_live and order.side == side and order.order_id != exclude_order_id)

    def cancel(self, order_id: int) -> bool:
        """Unknown IDs raise KeyError; cancellation of terminal orders returns False."""
        order = self.orders[order_id]
        cancelled = order.cancel()
        if not order.is_live:
            self._live_orders.pop(order_id, None)
        return cancelled

    def process_trade(
        self, trade: Trade, on_applied: Callable[[Fill], None] | None = None,
    ) -> list[Fill]:
        """Apply all fills atomically relative to strategy; optional hook records each."""
        # Also prune terminal orders if callers changed a returned order directly.
        self._live_orders = {
            order_id: order for order_id, order in self._live_orders.items() if order.is_live
        }
        active = [order for order in self._live_orders.values() if order.is_active]
        fills = self.model.match(trade, active)
        for fill in fills:
            order = self.orders[fill.order_id]
            order.apply_fill(fill.quantity)
            if not order.is_live:
                self._live_orders.pop(order.order_id, None)
            self.portfolio.apply_fill(fill)
            if on_applied is not None:
                on_applied(fill)
        return fills
