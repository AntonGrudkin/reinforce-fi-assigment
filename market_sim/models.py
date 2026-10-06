"""Events use integer epoch nanoseconds; quantities are ETH, prices/cash USD."""

from dataclasses import dataclass, field
from enum import Enum
from math import ceil, floor, isfinite, ulp


Number = float | int | str


def as_float(value: Number) -> float:
    """Normalize numeric inputs (including legacy string inputs) to finite floats."""
    result = float(value)
    if not isfinite(result):
        raise ValueError("Numeric values must be finite")
    return result


def quantities_close(first: float, second: float) -> bool:
    """Allow four floating-point units of rounding error, without a fixed dust cutoff."""
    return abs(first - second) <= 4 * max(ulp(first), ulp(second))


def round_to_step(value: float, step: float, *, up: bool = False) -> float:
    """Round directionally to a decimal tick/quantity step, allowing quotient noise."""
    value, step = as_float(value), as_float(step)
    if step <= 0:
        raise ValueError("Step must be positive")
    units = as_float(value / step)
    nearest = round(units)
    # A billionth of a step also covers noise from upstream headroom subtraction.
    if abs(units - nearest) <= max(4 * ulp(units), 1e-9):
        units = nearest
    count = ceil(units) if up else floor(units)
    # Round the product to the step's decimal spelling, e.g. 3 * 0.1 -> 0.3.
    mantissa, _, exponent = str(step).partition('e')
    places = max(0, len(mantissa.partition('.')[2]) - int(exponent or 0))
    return round(count * step, places)


def milliseconds_to_ns(value: Number) -> int:
    """Convert milliseconds to integer ns, rejecting sub-ns durations beyond rounding noise."""
    nanoseconds = as_float(value) * 1_000_000
    nearest = round(nanoseconds)
    if nanoseconds < 0 or not quantities_close(nanoseconds, nearest):
        raise ValueError("Duration must be nonnegative and represent whole nanoseconds")
    return nearest


@dataclass(frozen=True)
class BookSnapshot:
    timestamp_ns: int
    bids: tuple[tuple[Number, Number], ...]  # (price, quantity), best first
    asks: tuple[tuple[Number, Number], ...]


@dataclass(frozen=True)
class Trade:
    timestamp_ns: int
    price: Number
    size: Number
    is_maker_ask: bool  # True: buyer aggressor; False: seller aggressor


@dataclass(frozen=True)
class FundingUpdate:
    timestamp_ns: int
    funding_rate: Number


@dataclass(frozen=True)
class FundingSettlement:
    """Generated internally at 00:00, 08:00 and 16:00 UTC."""

    timestamp_ns: int


@dataclass(frozen=True)
class TimerEvent:
    timestamp_ns: int
    name: str = "timer"


MarketEvent = BookSnapshot | Trade | FundingUpdate | FundingSettlement
Event = MarketEvent | TimerEvent


def event_key(event: Event) -> tuple[int, int]:
    """At ties, settle after book/rate updates, before trades and strategy timers."""
    priority = {BookSnapshot: 0, FundingUpdate: 1, FundingSettlement: 2, Trade: 3, TimerEvent: 4}
    return event.timestamp_ns, priority[type(event)]


@dataclass
class MarketState:
    timestamp_ns: int | None = None
    book: BookSnapshot | None = None
    trade: Trade | None = None
    funding: FundingUpdate | None = None

    def update(self, event: Event) -> None:
        self.timestamp_ns = event.timestamp_ns
        if isinstance(event, BookSnapshot):
            self.book = event
        elif isinstance(event, Trade):
            self.trade = event
        elif isinstance(event, FundingUpdate):
            self.funding = event

    @property
    def mark_price(self) -> float | None:
        """Use the latest known book midpoint, or last trade before any book."""
        if self.book and self.book.bids and self.book.asks:
            return (as_float(self.book.bids[0][0]) + as_float(self.book.asks[0][0])) / 2
        return as_float(self.trade.price) if self.trade else None


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING_NEW = "pending_new"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class LimitOrder:
    order_id: int
    side: Side
    price: Number
    original_quantity: Number
    placement_time_ns: int
    remaining_quantity: float = field(init=False)
    status: OrderStatus = field(default=OrderStatus.OPEN, init=False)
    activation_timestamp_ns: int | None = field(default=None, init=False)
    queue_ahead: float = field(default=0.0, init=False)
    rejection_reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.side = Side(self.side)
        self.price = as_float(self.price)
        self.original_quantity = as_float(self.original_quantity)
        if self.price <= 0:
            raise ValueError("Order price must be finite and positive")
        if self.original_quantity <= 0:
            raise ValueError("Order quantity must be finite and positive")
        self.remaining_quantity = self.original_quantity

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_live(self) -> bool:
        """A submitted order is live to the strategy even before exchange acceptance."""
        return self.is_active or self.status == OrderStatus.PENDING_NEW

    def cancel(self) -> bool:
        """Cancellation is immediate and idempotent; filled quantity is retained."""
        if not self.is_live:
            return False
        self.status = OrderStatus.CANCELLED
        return True

    def apply_fill(self, quantity: Number) -> None:
        if not self.is_active:
            raise ValueError("Cannot fill an inactive order")
        quantity = as_float(quantity)
        if quantity <= 0 or (quantity > self.remaining_quantity and not quantities_close(quantity, self.remaining_quantity)):
            raise ValueError("Fill must be positive and no larger than the remainder")
        self.remaining_quantity = 0.0 if quantities_close(quantity, self.remaining_quantity) else self.remaining_quantity - quantity
        self.status = (
            OrderStatus.FILLED if self.remaining_quantity == 0
            else OrderStatus.PARTIALLY_FILLED
        )


@dataclass(frozen=True)
class Fill:
    order_id: int
    side: Side
    price: Number
    quantity: Number
    timestamp_ns: int
    # Execution metadata does not change the economic identity of a fill.
    activation_timestamp_ns: int | None = field(default=None, compare=False)
    trade_price: float | None = field(default=None, compare=False)
    queue_ahead_before: float | None = field(default=None, compare=False)
    queue_ahead_after: float | None = field(default=None, compare=False)
    fill_reason: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "price", as_float(self.price))
        object.__setattr__(self, "quantity", as_float(self.quantity))
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("Fill price and quantity must be positive")


@dataclass
class Portfolio:
    """One instrument, flat initially; average-cost PnL and a cash-flow ledger.

    Cash includes trade notionals, fees and settled funding. This is bookkeeping,
    not a collateral/margin model. Negative fee rates represent maker rebates.
    """

    initial_cash: Number = 0
    maker_fee_rate: Number = 0
    cash: float = field(init=False)
    inventory: float = field(default=0.0, init=False)
    _entry_notional: float = field(default=0.0, init=False, repr=False)
    realized_pnl: float = field(default=0.0, init=False)
    trading_fees: float = field(default=0.0, init=False)
    funding_pnl: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.initial_cash = as_float(self.initial_cash)
        self.maker_fee_rate = as_float(self.maker_fee_rate)
        self.cash = self.initial_cash

    def apply_fill(self, fill: Fill) -> float:
        """Apply a fill and return its signed fee cost (negative for a rebate)."""
        signed_quantity = fill.quantity if fill.side == Side.BUY else -fill.quantity
        old_size = self.inventory
        new_size = old_size + signed_quantity
        if old_size * signed_quantity < 0 and quantities_close(abs(old_size), fill.quantity):
            new_size = 0.0  # An intended full close must not leave a rounding-sized position.
        if old_size == 0 or old_size * signed_quantity > 0:
            self._entry_notional += fill.quantity * fill.price
        else:
            closed_size = abs(old_size) if new_size == 0 else min(abs(old_size), fill.quantity)
            direction = 1.0 if old_size > 0 else -1.0
            # Retain entry notional, rather than repeatedly rounding an average price.
            closed_cost = (
                self._entry_notional if closed_size == abs(old_size)
                else self._entry_notional * closed_size / abs(old_size)
            )
            self.realized_pnl += (closed_size * fill.price - closed_cost) * direction
            self._entry_notional -= closed_cost
            if new_size == 0:
                self._entry_notional = 0.0
            elif old_size * new_size < 0:
                self._entry_notional = abs(new_size) * fill.price
        fee = fill.quantity * fill.price * self.maker_fee_rate
        self.inventory = new_size
        self.trading_fees += fee
        self.cash -= signed_quantity * fill.price + fee
        return fee

    def apply_funding(self, mark_price: Number | None, funding_rate: Number | None) -> float:
        """Return signed cash change: positive rates charge longs and credit shorts."""
        if self.inventory == 0:
            return 0.0
        if mark_price is None or funding_rate is None:
            raise ValueError("Funding settlement for an open position requires a known price and rate")
        payment = -self.inventory * as_float(mark_price) * as_float(funding_rate)
        self.cash += payment
        self.funding_pnl += payment
        return payment

    @property
    def average_entry_price(self) -> float:
        return self._entry_notional / abs(self.inventory) if self.inventory else 0.0

    @property
    def net_realized_pnl(self) -> float:
        """Gross realized trading PnL minus all paid fees plus settled funding."""
        return self.realized_pnl - self.trading_fees + self.funding_pnl

    def position_balance(self, mark_price: Number | None) -> float:
        if self.inventory == 0:
            return 0.0
        if mark_price is None:
            raise ValueError("A mark price is required for nonzero inventory")
        return self.inventory * as_float(mark_price)

    def unrealized_pnl(self, mark_price: Number | None) -> float:
        signed_cost = self._entry_notional if self.inventory >= 0 else -self._entry_notional
        return self.position_balance(mark_price) - signed_cost

    def equity(self, mark_price: Number | None) -> float:
        return self.cash + self.position_balance(mark_price)

    def pnl(self, mark_price: Number | None) -> float:
        """Total marked PnL, including fees and physically settled funding."""
        return self.equity(mark_price) - self.initial_cash
