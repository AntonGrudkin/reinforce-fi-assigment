"""Midpoint quotes, linear inventory size control and optional RV width scaling."""

from dataclasses import dataclass, field
from math import isfinite, tanh

from market_sim import (
    BacktestEngine, BookSnapshot, FundingSettlement, LimitOrder, MarketEvent,
    Side, Strategy, Trade,
)
from market_sim.models import as_float, milliseconds_to_ns, round_to_step

if __package__:
    from .signals import TradeFlowSignal
else:
    from signals import TradeFlowSignal


HARD_INVENTORY_FRACTION = 0.2


@dataclass(frozen=True)
class QuoteTargets:
    instrument_price: float | None
    actual_inventory: float
    max_position_size: float | None
    normalized_inventory: float | None
    half_spread_ticks: float
    center_price: float | None


@dataclass
class NeutralTrendGateStrategy(Strategy):
    """Preserve the prototype without its unused alpha controllers.

    The historical name remains, but there is no threshold-based trend gate.
    Positive trend lowers quotes; inventory affects sizes, not baseline prices.
    """

    tick_size: float = 0.01
    quantity_step: float = 0.001
    order_fraction: float = 0.01
    half_life_seconds: float = 300.0
    target_annual_volatility: float = 0.5
    half_spread_ticks_at_target: float = 25.0
    trend_skew_strength: float = 0.5
    adjust_width_to_volatility: bool = True
    requote_interval_ms: float = 500.0
    warmup_seconds: float = 3600.0
    quotes: dict[Side, LimitOrder] = field(default_factory=dict, init=False)
    signal: TradeFlowSignal = field(init=False)
    _requote_interval_ns: int = field(init=False)
    _warmup_ns: int = field(init=False)
    _warmup_start_ns: int | None = field(default=None, init=False)
    _last_action_ns: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        for name in ('tick_size', 'quantity_step', 'order_fraction', 'half_life_seconds',
                     'target_annual_volatility', 'half_spread_ticks_at_target'):
            value = as_float(getattr(self, name))
            if value <= 0:
                raise ValueError(f'{name} must be positive')
            setattr(self, name, value)
        if self.order_fraction > 1:
            raise ValueError('Order fraction must be no larger than 1')
        for name in ('trend_skew_strength', 'warmup_seconds'):
            value = as_float(getattr(self, name))
            if value < 0:
                raise ValueError(f'{name} must be nonnegative')
            setattr(self, name, value)
        if not isinstance(self.adjust_width_to_volatility, bool):
            raise ValueError('adjust_width_to_volatility must be a boolean')
        self._requote_interval_ns = milliseconds_to_ns(self.requote_interval_ms)
        self._warmup_ns = round(self.warmup_seconds * 1_000_000_000)
        self.signal = TradeFlowSignal(self.half_life_seconds)

    def on_start(self, runtime: BacktestEngine) -> None:
        self.quotes.clear()
        self._last_action_ns = self._warmup_start_ns = None
        self.signal = TradeFlowSignal(self.half_life_seconds)

    @property
    def half_spread_ticks(self) -> float:
        volatility = self.signal.annualized_volatility
        if not self.adjust_width_to_volatility or volatility is None:
            return self.half_spread_ticks_at_target
        return max(1.0, self.half_spread_ticks_at_target * volatility / self.target_annual_volatility)

    def _snapshot(self, runtime: BacktestEngine) -> QuoteTargets:
        book, mark = runtime.state.book, runtime.state.mark_price
        equity, actual = runtime.equity, runtime.portfolio.inventory
        half_width = self.half_spread_ticks
        budget = normalized = center = None
        if book is not None and book.bids and book.asks:
            bid, ask = as_float(book.bids[0][0]), as_float(book.asks[0][0])
            if 0 < bid < ask and mark is not None and mark > 0 and equity > 0:
                budget = HARD_INVENTORY_FRACTION * equity / mark
                normalized = max(-1.0, min(1.0, actual / budget))
                trend = self.signal.trend_magnitude
                trend = trend if trend is not None and isfinite(trend) else 0.0
                pressure = self.trend_skew_strength * abs(normalized) * tanh(trend)
                center = 0.5 * bid + 0.5 * ask - half_width * pressure * self.tick_size
        return QuoteTargets(mark, actual, budget, normalized, half_width, center)

    def _target_quantities(self, equity: float, mark: float, normalized: float) -> dict[Side, float]:
        """Transfer increasing-side size to reducing side, keeping rounded total."""
        base = round_to_step(equity * self.order_fraction / mark, self.quantity_step)
        increasing = Side.BUY if normalized >= 0 else Side.SELL
        reducing = Side.SELL if normalized >= 0 else Side.BUY
        size = round_to_step(base * (1 - abs(normalized)), self.quantity_step)
        return {increasing: size, reducing: round_to_step(2 * base - size, self.quantity_step)}

    def on_market_event(self, event: MarketEvent, runtime: BacktestEngine) -> None:
        # Runtime matches/accounting first; this print enters the signal afterward.
        if isinstance(event, Trade):
            if not self.signal.update(event):
                return
        elif not isinstance(event, (BookSnapshot, FundingSettlement)):
            return
        self._requote(runtime, self._snapshot(runtime))

    def _requote(self, runtime: BacktestEngine, view: QuoteTargets) -> None:
        timestamp = runtime.state.timestamp_ns
        if self._warmup_ns:
            if self.signal.fair_price is None:
                return
            if self._warmup_start_ns is None:
                self._warmup_start_ns = timestamp
            if (timestamp - self._warmup_start_ns < self._warmup_ns
                    or self.signal.annualized_volatility is None):
                return
        runtime.set_inventory_limit(HARD_INVENTORY_FRACTION, quantity_step=self.quantity_step)
        if self._last_action_ns is not None and timestamp - self._last_action_ns < self._requote_interval_ns:
            return
        if view.center_price is None or view.max_position_size is None:
            for side in Side:
                self._cancel(runtime, side)
            return
        book = runtime.state.book
        bid, ask = as_float(book.bids[0][0]), as_float(book.asks[0][0])
        half_spread = view.half_spread_ticks * self.tick_size
        prices = {
            Side.BUY: round_to_step(view.center_price - half_spread, self.tick_size),
            Side.SELL: round_to_step(view.center_price + half_spread, self.tick_size, up=True),
        }
        headroom = {Side.BUY: max(0.0, view.max_position_size - view.actual_inventory),
                    Side.SELL: max(0.0, view.max_position_size + view.actual_inventory)}
        quantities = self._target_quantities(runtime.equity, view.instrument_price, view.normalized_inventory)
        for side in Side:
            quantity = round_to_step(min(quantities[side], headroom[side]), self.quantity_step)
            passive = prices[side] < ask if side == Side.BUY else prices[side] > bid
            if quantity == 0 or prices[side] <= 0 or not passive:
                self._cancel(runtime, side)
                continue
            current = self.quotes.get(side)
            if current and current.is_live and current.price == prices[side] and current.remaining_quantity == quantity:
                continue
            self._cancel(runtime, side)
            self.quotes[side] = runtime.place_order(side, prices[side], quantity)
            self._last_action_ns = timestamp

    def _cancel(self, runtime: BacktestEngine, side: Side) -> None:
        current = self.quotes.pop(side, None)
        if current and current.is_live and runtime.cancel_order(current.order_id):
            self._last_action_ns = runtime.state.timestamp_ns
