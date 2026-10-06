"""One research grid evaluation, streaming histories and event-level metrics.

Imported by grid_neutral_staged.py; no evaluations run on import. The staged
search schedules this top-level function so multiprocessing works on macOS.
"""

import csv
import json
import os
from contextlib import ExitStack
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev
from time import perf_counter

from market_sim import load_window, Side
from market_sim.history import OrderEvent, PositionSnapshot

if __package__:
    from .in_sample_analysis import AuditedBacktest, FILL_COLUMNS, LastRecord
    from .strategy import NeutralTrendGateStrategy
else:
    from in_sample_analysis import AuditedBacktest, FILL_COLUMNS, LastRecord
    from strategy import NeutralTrendGateStrategy


START_DATE = '2026-03-19'
N_HOURS = 24.0
WORKERS = min(6, os.cpu_count() or 1)
RETURN_INTERVAL_MINUTES = 15
NANOSECONDS = 1_000_000_000
PLOT_SAMPLE_SECONDS = 1.0
FIXED_PARAMETERS = dict(order_fraction=0.01, requote_interval_ms=500.0,
                        target_annual_volatility=0.5, warmup_seconds=3600.0,
                        adjust_width_to_volatility=False)


@dataclass(frozen=True)
class GridSignalSnapshot:
    timestamp_ns: int
    instrument_price: float | None
    actual_inventory: float
    max_position_size: float | None
    normalized_inventory: float | None
    half_spread_ticks: float
    center_price: float | None
    fair_price: float | None
    annualized_volatility: float | None
    trend_magnitude: float | None
    placement_width_ticks: float
    buy_quantity: float | None
    sell_quantity: float | None


class RecordingStrategy(NeutralTrendGateStrategy):
    """Add sampled grid diagnostics without changing strategy decisions."""

    signal_sink = None

    def _snapshot(self, runtime):
        view = super()._snapshot(runtime)
        if self.signal_sink is not None:
            quantities = (self._target_quantities(runtime.equity, view.instrument_price,
                                                  view.normalized_inventory)
                          if view.normalized_inventory is not None else {})
            self.signal_sink.append(GridSignalSnapshot(
                timestamp_ns=runtime.state.timestamp_ns, **vars(view),
                fair_price=self.signal.fair_price,
                annualized_volatility=self.signal.annualized_volatility,
                trend_magnitude=self.signal.trend_magnitude,
                placement_width_ticks=2 * view.half_spread_ticks,
                buy_quantity=quantities.get(Side.BUY), sell_quantity=quantities.get(Side.SELL)))
        return view


def window_bounds(day: str, hours: float = N_HOURS) -> tuple[int, int]:
    start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()) * NANOSECONDS
    return start, start + round(hours * 3600 * NANOSECONDS)


class CsvHistory(LastRecord):
    """Stream dataclass rows; always retain the latest unsampled row for callbacks.

    Sampling matches the plots: first row, last observation per elapsed-time
    bucket and final row. Actual timestamps stay unchanged. flush() writes the
    final pending observation. No sampling means every lifecycle row is written.
    """

    def __init__(self, writer, record_type, sample_seconds: float | None = None):
        super().__init__()
        self.writer = writer
        self.names = [field.name for field in fields(record_type)]
        writer.writerow(self.names)
        self.interval_ns = round(sample_seconds * NANOSECONDS) if sample_seconds is not None else None
        if self.interval_ns is not None and self.interval_ns <= 0:
            raise ValueError('Sampling interval must be positive')
        self.start_ns = self.bucket = self.first = self.pending = None

    def _write(self, record):
        values = [getattr(record, name) for name in self.names]
        self.writer.writerow([value.value if isinstance(value, Enum) else value for value in values])

    def append(self, record):
        timestamp = record.timestamp_ns
        if self.last is not None and self.last.timestamp_ns is not None:
            if timestamp is None or timestamp < self.last.timestamp_ns:
                raise ValueError('History timestamps must be chronological')
        super().append(record)
        if self.interval_ns is None or timestamp is None:
            self._write(record)
            return
        if self.start_ns is None:
            self.start_ns, self.first = timestamp, record
            self._write(record)
        bucket = (timestamp - self.start_ns) // self.interval_ns
        if self.bucket is not None and bucket != self.bucket and self.pending is not self.first:
            self._write(self.pending)
        self.bucket, self.pending = bucket, record

    def flush(self):
        if self.pending is not None and self.pending is not self.first:
            self._write(self.pending)
        self.first = self.pending = None


class EquityMetrics(LastRecord):
    """Event-level risk metrics plus causal quarter-hour closes; bounded memory."""

    def __init__(self, start_ns: int, end_ns: int, interval_minutes: int = RETURN_INTERVAL_MINUTES,
                 history_sink: CsvHistory | None = None):
        super().__init__()
        self.start_ns, self.end_ns = start_ns, end_ns
        self.interval_ns = interval_minutes * 60 * NANOSECONDS
        if self.interval_ns <= 0 or end_ns <= start_ns:
            raise ValueError('Window and return interval must be positive')
        self.next_close_ns = start_ns + self.interval_ns
        self.equity_closes = []
        self.peak = self.max_drawdown = self.max_drawdown_fraction = 0.0
        self.min_unrealized_pnl_equity_fraction = None
        self.history_sink = history_sink

    def append(self, row):
        if row.event == 'initial':
            self.equity_closes.append(row.equity)
            self.peak = row.equity
        else:
            if not self.start_ns <= row.timestamp_ns <= self.end_ns:
                raise ValueError('Accounting timestamp is outside the grid window')
            if self.last.timestamp_ns is not None and row.timestamp_ns < self.last.timestamp_ns:
                raise ValueError('Accounting timestamps must be chronological')
            # Strict < lets all equal-time funding/fills/commands finish a close.
            # Gaps carry previous equity; a future row never sets an earlier close.
            while self.next_close_ns < row.timestamp_ns:
                self.equity_closes.append(self.last.equity)
                self.next_close_ns += self.interval_ns
            self.peak = max(self.peak, row.equity)
            drawdown = self.peak - row.equity
            self.max_drawdown = max(self.max_drawdown, drawdown)
            self.max_drawdown_fraction = max(self.max_drawdown_fraction, drawdown / self.peak)
        if row.equity != 0:
            ratio = row.unrealized_pnl / row.equity
            self.min_unrealized_pnl_equity_fraction = (
                ratio if self.min_unrealized_pnl_equity_fraction is None
                else min(self.min_unrealized_pnl_equity_fraction, ratio))
        super().append(row)
        if self.history_sink is not None:
            self.history_sink.append(row)

    def report(self) -> dict:
        # Includes endpoint funding and equal-time command completions.
        while self.next_close_ns <= self.end_ns:
            self.equity_closes.append(self.last.equity)
            self.next_close_ns += self.interval_ns
        if any(equity <= 0 for equity in self.equity_closes):
            raise ValueError('Equity returns require positive sampled equity')
        returns = [closing / opening - 1 for opening, closing
                   in zip(self.equity_closes, self.equity_closes[1:])]
        periods_per_year = 365 * 24 * 3600 * NANOSECONDS / self.interval_ns
        mu = fmean(returns) * periods_per_year if returns else None
        sigma = stdev(returns) * sqrt(periods_per_year) if len(returns) > 1 else None
        return dict(total_pnl_usd=self.last.equity - self.equity_closes[0],
                    final_equity_usd=self.last.equity, max_drawdown_usd=self.max_drawdown,
                    max_drawdown_fraction=self.max_drawdown_fraction,
                    min_unrealized_pnl_equity_fraction=self.min_unrealized_pnl_equity_fraction,
                    annualized_mu=mu, annualized_sigma=sigma,
                    sharpe=mu / sigma if sigma is not None and sigma > 0 else None,
                    return_samples=len(returns))


def run_case(parameters: dict[str, float], *, data: Path, output: Path, run_id: int,
             day: str = START_DATE, hours: float = N_HOURS,
             fixed_parameters: dict[str, float] | None = None) -> dict:
    started = perf_counter()
    events, total = load_window(data, day, hours)
    start, requested_end = window_bounds(day, hours)
    end = events.end_timestamp_ns
    effective_hours = (end - start) / (3600 * NANOSECONDS)
    directory = output / f'run_{run_id:03d}'
    directory.mkdir(parents=True, exist_ok=False)
    strategy_parameters = {**FIXED_PARAMETERS, **(fixed_parameters or {}), **parameters}
    strategy = RecordingStrategy(**strategy_parameters)
    settings = dict(**strategy_parameters, start_date=day, hours=hours,
                    effective_hours=effective_hours, window_start_ns=start, window_end_ns=end,
                    requested_end_ns=requested_end, window_trimmed=end < requested_end,
                    initial_cash_usd=10000.0, maker_fee_rate=0.0002, latency_ms=50.0,
                    tick_size=strategy.tick_size, quantity_step=strategy.quantity_step,
                    hard_inventory_fraction=0.2, plot_sample_seconds=PLOT_SAMPLE_SECONDS)
    (directory / 'parameters.json').write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    with ExitStack() as stack:
        def writer(filename):
            return csv.writer(stack.enter_context((directory / filename).open('x', newline='', encoding='utf-8')))

        fills = csv.DictWriter(stack.enter_context(
            (directory / 'fills.csv').open('x', newline='', encoding='utf-8')), fieldnames=FILL_COLUMNS)
        fills.writeheader()
        runtime = AuditedBacktest(strategy, fills)
        history = CsvHistory(writer('history.csv'), PositionSnapshot, PLOT_SAMPLE_SECONDS)
        orders = CsvHistory(writer('orders.csv'), OrderEvent)
        signals = CsvHistory(writer('signals.csv'), GridSignalSnapshot, PLOT_SAMPLE_SECONDS)
        runtime.history = EquityMetrics(start, end, history_sink=history)
        runtime.order_history = orders
        strategy.signal_sink = signals
        runtime.run(runtime.counted_events(events), end_timestamp_ns=end)
        if runtime.market_event_count != total:
            raise AssertionError('Processed event count differs from the requested window')
        for sink in (history, orders, signals):
            sink.flush()
        report = runtime.history.report()
        returns_writer = writer('returns_15min.csv')
        returns_writer.writerow(('timestamp_ns', 'equity', 'return_fraction'))
        for i, equity in enumerate(runtime.history.equity_closes):
            previous = runtime.history.equity_closes[i - 1] if i else None
            returns_writer.writerow((start + i * runtime.history.interval_ns, equity,
                                     equity / previous - 1 if previous is not None else None))
    row = dict(run_id=run_id, run_directory=directory.name, **strategy_parameters,
               window_start_utc=datetime.fromtimestamp(start // NANOSECONDS, timezone.utc).isoformat(),
               window_end_utc=datetime.fromtimestamp(end / NANOSECONDS, timezone.utc).isoformat(),
               window_start_ns=start, window_end_ns=end, requested_hours=hours,
               effective_hours=effective_hours, window_trimmed=end < requested_end,
               initial_cash_usd=10000.0, maker_fee_rate=runtime.portfolio.maker_fee_rate, latency_ms=50.0,
               market_events=runtime.market_event_count - 1, n_fills=runtime.n_fills,
               traded_notional_usd=runtime.traded_notional,
               turnover=runtime.traded_notional / runtime.history.equity_closes[0],
               **report, return_interval_minutes=RETURN_INTERVAL_MINUTES,
               trading_fees_usd=runtime.portfolio.trading_fees, funding_pnl_usd=runtime.portfolio.funding_pnl,
               execution_pnl_usd=runtime._attribution.execution_pnl,
               inventory_pnl_usd=runtime._attribution.inventory_pnl,
               final_position_size=runtime.portfolio.inventory,
               max_fill_attribution_error=runtime.max_audit_error,
               wall_seconds=perf_counter() - started)
    (directory / 'metrics.json').write_text(json.dumps(row, indent=2) + '\n', encoding='utf-8')
    return row

