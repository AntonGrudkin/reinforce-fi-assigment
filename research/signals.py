"""Trade-derived RV and trend, preserved from the EMA prototype."""

from dataclasses import dataclass, field
from math import exp, isfinite, log, sqrt

from market_sim import Trade
from market_sim.models import as_float

YEAR_NS = 365 * 24 * 60 * 60 * 1_000_000_000


@dataclass
class TradeFlowSignal:
    """Normalized exponentially decaying trade-price mean and local realized vol.

    Price weights are 1 or trade size, and old weights halve every half-life.
    RV = sqrt(year_ns * decayed squared log returns / decayed elapsed ns).
    Same-time trades all affect fair price; for RV they revise a single interval's
    endpoint using only prints seen so far. Vol is unavailable before two distinct
    timestamps. Neither estimator uses book prices or future trades.
    """

    half_life_seconds: float = 3600.0
    size_weighted: bool = False
    fair_price: float | None = field(default=None, init=False)
    annualized_volatility: float | None = field(default=None, init=False)
    _last_timestamp_ns: int | None = field(default=None, init=False)
    _last_price: float | None = field(default=None, init=False)
    _price_sum: float = field(default=0.0, init=False)
    _weight_sum: float = field(default=0.0, init=False)
    _squared_returns: float = field(default=0.0, init=False)
    _log_returns: float = field(default=0.0, init=False)
    _elapsed_ns: float = field(default=0.0, init=False)
    _interval_start_price: float | None = field(default=None, init=False)
    _interval_duration_ns: float = field(default=0.0, init=False)
    _interval_decay: float = field(default=1.0, init=False)
    _previous_squared_returns: float = field(default=0.0, init=False)
    _previous_log_returns: float = field(default=0.0, init=False)
    _previous_elapsed_ns: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.half_life_seconds = as_float(self.half_life_seconds)
        if self.half_life_seconds <= 0:
            raise ValueError("Half-life must be positive")

    def update(self, trade: Trade) -> bool:
        """Return False for zero-size prints; reject invalid or regressing inputs."""
        price, size = as_float(trade.price), as_float(trade.size)
        if price <= 0 or size < 0:
            raise ValueError("Trade price must be positive and size nonnegative")
        if self._last_timestamp_ns is not None and trade.timestamp_ns < self._last_timestamp_ns:
            raise ValueError("Trade timestamps must be chronological")
        if size == 0:
            return False
        elapsed = trade.timestamp_ns - self._last_timestamp_ns if self._last_timestamp_ns is not None else 0
        decay = exp(-log(2) * elapsed / (self.half_life_seconds * 1_000_000_000))
        weight = size if self.size_weighted else 1.0
        self._price_sum = decay * self._price_sum + weight * price
        self._weight_sum = decay * self._weight_sum + weight
        self.fair_price = self._price_sum / self._weight_sum

        if elapsed > 0:
            self._interval_start_price = self._last_price
            self._interval_duration_ns = float(elapsed)
            self._interval_decay = decay
            self._previous_squared_returns = self._squared_returns
            self._previous_log_returns = self._log_returns
            self._previous_elapsed_ns = self._elapsed_ns
        if self._interval_start_price is not None:
            log_return = log(price / self._interval_start_price)
            self._squared_returns = self._interval_decay * self._previous_squared_returns + log_return ** 2
            self._log_returns = self._interval_decay * self._previous_log_returns + log_return
            self._elapsed_ns = self._interval_decay * self._previous_elapsed_ns + self._interval_duration_ns
            self.annualized_volatility = sqrt(YEAR_NS * self._squared_returns / self._elapsed_ns)
        self._last_timestamp_ns, self._last_price = trade.timestamp_ns, price
        return True

    @property
    def trend_magnitude(self) -> float | None:
        """Signed EW log-price change / RV, both scaled to one half-life.

        Drift and RV share decay weights and elapsed return intervals. This is
        an EW estimate, not a fixed-window endpoint return; same-time prints
        revise one interval, just as for RV. No price-level volatility is used.
        """
        volatility = self.annualized_volatility
        if self._elapsed_ns <= 0 or volatility is None:
            return None
        horizon_vol = volatility * sqrt(self.half_life_seconds * 1_000_000_000 / YEAR_NS)
        if not isfinite(horizon_vol) or horizon_vol <= 1e-12:
            return None
        drift_per_second = self._log_returns / (self._elapsed_ns / 1_000_000_000)
        return drift_per_second * self.half_life_seconds / horizon_vol


