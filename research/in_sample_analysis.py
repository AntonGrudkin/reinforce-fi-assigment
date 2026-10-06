"""Generate in-sample fill CSVs only: python research/in_sample_analysis.py.

Each configured full width runs with RV adjustment off and on. Plot saved CSVs
separately using research/plot_in_sample.py. No full market/signal histories are
exported; pre-fill RV/trend and closing PnL are retained for the two analyses.
"""

import csv
import gc
import json
from datetime import datetime, timezone
from math import isclose, isfinite
from pathlib import Path

from market_sim import BacktestEngine, Side, TradeExecutionModel, load_window
from market_sim.data import NANOSECONDS
from market_sim.models import as_float

if __package__:
    from .strategy import NeutralTrendGateStrategy
else:
    from strategy import NeutralTrendGateStrategy


DAY = '2026-03-19'
N_HOURS = 24.0
VOLATILITY_ADJUSTMENTS = (False, True)  # Set to (False,) or (True,) for one mode.
FULL_WIDTHS_TICKS = (50, 75, 100, 150, 200, 300, 400)
STRATEGY_PARAMETERS = dict(
    half_life_seconds=300.0,
    target_annual_volatility=0.5,
    warmup_seconds=3600.0,
    trend_skew_strength=0.5,
    order_fraction=0.01,
    requote_interval_ms=500.0,
)
RESULT_COLUMNS = ('full_width_ticks', 'n_fills', 'traded_notional_usd',
                  'execution_pnl', 'fees', 'execution_minus_fees')
FILL_COLUMNS = (
    'fill_number', 'timestamp_ns', 'book_timestamp_ns', 'order_id', 'side',
    'pre_fill_mid', 'fill_price', 'quantity', 'fee', 'recorded_execution_pnl',
    'adjust_width_to_volatility', 'full_width_ticks',
    'full_quote_width_ticks_at_placement', 'half_spread_ticks_before_fill',
    'annualized_volatility', 'trend_magnitude', 'position_effect',
    'average_entry_price_before_fill', 'realized_pnl_from_fill',
    'position_size_before_fill', 'position_size_after_fill', 'position_notional_usd',
)


class LastRecord:
    """Append-only sink with a cumulative length and access to the latest record."""

    def __init__(self):
        self.count = 0
        self.last = None

    def append(self, record):
        self.count += 1
        self.last = record

    def clear(self):
        self.count = 0
        self.last = None

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index != -1 or self.last is None:
            raise IndexError('Only the most recent record is retained')
        return self.last


class PreFillAuditModel(TradeExecutionModel):
    """Capture book and trade signals before matching the current public trade."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.pre_book = None
        self.pre_volatility = None
        self.pre_trend_magnitude = None
        self.pre_half_spread_ticks = None

    def match(self, trade, orders):
        self.pre_book = self.runtime.state.book
        # The strategy incorporates this trade only after all its fills/callbacks.
        # Capture once so every fill from the same print has identical features.
        signal = getattr(self.runtime.strategy, 'signal', None)
        self.pre_volatility = getattr(signal, 'annualized_volatility', None)
        self.pre_trend_magnitude = getattr(signal, 'trend_magnitude', None)
        self.pre_half_spread_ticks = getattr(self.runtime.strategy, 'half_spread_ticks', None)
        return super().match(trade, orders)


class AuditedBacktest(BacktestEngine):
    def __init__(self, strategy, writer=None):
        super().__init__(strategy, initial_cash=10000, maker_fee_rate=0.0002, latency_ms=50)
        self.history = LastRecord()
        self.order_history = LastRecord()
        self.execution.model = PreFillAuditModel(self)
        self.writer = writer
        self.n_fills = 0
        self.traded_notional = 0.0
        self.expected_execution = 0.0
        self.max_audit_error = 0.0
        self.market_event_count = 0
        self._placement_widths = {}

    def place_order(self, side, price, quantity):
        # Retain the submitted target width: resting orders can fill after RV changes.
        half_width = getattr(self.strategy, 'half_spread_ticks', None)
        order = super().place_order(side, price, quantity)
        self._placement_widths[order.order_id] = 2 * half_width if half_width is not None else None
        return order

    def _record_fill(self, fill):
        # Called after accounting applies this fill, but before its snapshot.
        # The last snapshot is the position after the previous event/fill, even
        # when one public trade fills several orders sequentially.
        before = self.history[-1]
        model = self.execution.model
        book = model.pre_book
        if book is None or not book.bids or not book.asks:
            raise AssertionError('Every audited fill requires a pre-match BBO')
        bid, ask = as_float(book.bids[0][0]), as_float(book.asks[0][0])
        pre_mid = (bid + ask) / 2
        assert book.timestamp_ns <= fill.timestamp_ns
        signed_quantity = fill.quantity if fill.side == Side.BUY else -fill.quantity
        expected = (pre_mid - fill.price) * signed_quantity
        previous_execution = self._attribution.execution_pnl
        super()._record_fill(fill)
        record = self.history[-1]
        position_effect = 'increase' if abs(record.position_size) > abs(before.position_size) else 'decrease'
        entry_before = realized_from_fill = None
        if position_effect == 'decrease':
            entry_before = before.average_entry_price
            # Gross ledger increment: (entry - fill price) * signed CLOSED qty,
            # with BUY positive / SELL negative. Reversal's new leg realizes zero.
            realized_from_fill = record.realized_pnl - before.realized_pnl
        recorded = record.execution_pnl - previous_execution
        error = recorded - expected
        assert record.instrument_price == pre_mid
        assert isclose(recorded, expected, rel_tol=1e-10, abs_tol=1e-9), (fill, pre_mid, expected, recorded)
        self.n_fills += 1
        self.traded_notional += fill.quantity * fill.price
        self.expected_execution += expected
        self.max_audit_error = max(self.max_audit_error, abs(error))
        if self.writer is not None:
            self.writer.writerow(dict(
                fill_number=self.n_fills, timestamp_ns=fill.timestamp_ns,
                book_timestamp_ns=book.timestamp_ns, order_id=fill.order_id,
                side=fill.side.value, pre_fill_mid=pre_mid,
                fill_price=fill.price, quantity=fill.quantity, fee=record.fee,
                recorded_execution_pnl=recorded,
                adjust_width_to_volatility=self.strategy.adjust_width_to_volatility,
                full_width_ticks=2 * self.strategy.half_spread_ticks_at_target,
                full_quote_width_ticks_at_placement=self._placement_widths[fill.order_id],
                half_spread_ticks_before_fill=model.pre_half_spread_ticks,
                annualized_volatility=model.pre_volatility,
                trend_magnitude=model.pre_trend_magnitude,
                position_effect=position_effect,
                average_entry_price_before_fill=entry_before,
                realized_pnl_from_fill=realized_from_fill,
                position_size_before_fill=before.position_size,
                position_size_after_fill=record.position_size,
                position_notional_usd=record.position_balance,
            ))

    def counted_events(self, events):
        for event in events:
            self.market_event_count += 1
            yield event

    def five_metrics(self, full_width):
        execution, fees = self._attribution.execution_pnl, self.portfolio.trading_fees
        assert isclose(self.expected_execution, execution, rel_tol=1e-10, abs_tol=1e-8)
        assert isclose(fees, self.traded_notional * self.portfolio.maker_fee_rate, rel_tol=1e-10, abs_tol=1e-8)
        return dict(zip(RESULT_COLUMNS, (full_width, self.n_fills, self.traded_notional,
                                         execution, fees, execution - fees)))


def mode_name(adjust: bool) -> str:
    return 'with_vol_adjustment' if adjust else 'without_vol_adjustment'


def main(*, data: Path | None = None, output: Path | None = None,
         hours: float = N_HOURS, progress: bool = True) -> Path:
    """Run comparisons and stream fills; plotting is deliberately a separate step."""
    if not FULL_WIDTHS_TICKS or any(not isfinite(w) or w <= 0 for w in FULL_WIDTHS_TICKS):
        raise ValueError('Configure positive finite full widths')
    if not VOLATILITY_ADJUSTMENTS or any(type(value) is not bool for value in VOLATILITY_ADJUSTMENTS):
        raise ValueError('Configure volatility adjustment modes as booleans')
    if len(set(VOLATILITY_ADJUSTMENTS)) != len(VOLATILITY_ADJUSTMENTS):
        raise ValueError('Volatility adjustment modes must be unique')
    root = Path(__file__).resolve().parents[1]
    data = data if data is not None else root / 'data'
    if output is None:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        output = root / 'figures' / f'in_sample_{DAY}_{stamp}'
    # Fresh directories and exclusive file creation prevent concurrent overwrite.
    output.mkdir(parents=True, exist_ok=False)
    print(f'Reports: {output}', flush=True)
    metadata = dict(status='running', start_date=DAY, requested_hours=hours,
                    strategy_parameters=STRATEGY_PARAMETERS, full_widths_ticks=FULL_WIDTHS_TICKS,
                    volatility_adjustments=VOLATILITY_ADJUSTMENTS,
                    initial_cash_usd=10000, maker_fee_rate=0.0002, latency_ms=50,
                    rv_timing='Immediately before matching; triggering trade excluded',
                    fill_rows='Individual executions, including partial fills; IDs unique within (mode, width)',
                    realized_pnl='Gross average-cost ledger increment on decreasing fills; fees excluded',
                    width_definition='Nominal full width; at target RV when adjustment is on, fixed otherwise')
    metadata_path = output / 'metadata.json'
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    with (output / 'summary.csv').open('x', newline='', encoding='utf-8') as source:
        summary = csv.DictWriter(source, fieldnames=('adjust_width_to_volatility', *RESULT_COLUMNS))
        summary.writeheader()
        for adjust in VOLATILITY_ADJUSTMENTS:
            directory = output / mode_name(adjust)
            directory.mkdir()
            with (directory / 'fills.csv').open('x', newline='', encoding='utf-8') as fill_file:
                writer = csv.DictWriter(fill_file, fieldnames=FILL_COLUMNS)
                writer.writeheader()
                for width in FULL_WIDTHS_TICKS:
                    events, total = load_window(data, DAY, hours)
                    strategy = NeutralTrendGateStrategy(
                        **{**STRATEGY_PARAMETERS, 'half_spread_ticks_at_target': width / 2,
                           'adjust_width_to_volatility': adjust})
                    runtime = AuditedBacktest(strategy, writer)
                    runtime.run(runtime.counted_events(events), progress=progress, total_events=total,
                                end_timestamp_ns=events.end_timestamp_ns)
                    assert runtime.market_event_count == total
                    metrics = dict(adjust_width_to_volatility=adjust, **runtime.five_metrics(width))
                    summary.writerow(metrics)
                    source.flush()
                    fill_file.flush()
                    print(json.dumps(metrics), flush=True)
                    metadata.update(window_start_ns=events.start_timestamp_ns,
                                    window_end_ns=events.end_timestamp_ns,
                                    effective_hours=(events.end_timestamp_ns - events.start_timestamp_ns) / (3600 * NANOSECONDS),
                                    window_trimmed=events.end_timestamp_ns < events.requested_end_timestamp_ns)
                    del runtime, strategy
                    gc.collect()
    metadata['status'] = 'complete'
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(f'CSV generation complete. Plot with: python research/plot_in_sample.py {output}', flush=True)
    return output


if __name__ == '__main__':
    main()
