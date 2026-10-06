"""Redraw the combined frozen-backtest dashboard using saved CSVs only."""

import json
from datetime import datetime, timezone
from pathlib import Path

from market_sim.history import load_order_history
from market_sim.models import Side
from market_sim.plotting import (
    _add_price_overlay, _read_plot_rows, _resting_segments,
    _sample_by_time, _sample_interval_ns,
)

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'figures/frozen_backtest/run_001'
OUTPUT = ROOT / 'figures/frozen_backtest/history.png'
# Half-open UTC research partitions; clip shading to the observed data interval.
REGIONS = (
    ('Train', '2026-03-19', '2026-03-20', '#91b9ef'),
    ('Validation', '2026-03-20', '2026-03-21', '#efcd82'),
    ('OOS', '2026-03-21', '2026-03-22', '#a2d4a1'),
)
HOUR_NS = 3_600_000_000_000


def utc_ns(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()) * 1_000_000_000


def plot_frozen_backtest(directory: Path = DIRECTORY, output: Path = OUTPUT,
                         *, sample_interval_seconds: float = 1.0) -> Path:
    """Accounting, actual order lifecycle and target quote components in one PNG.

    Attribution is deliberately omitted. CSVs are unchanged; placements and
    reconstructed resting spans are sampled, while every fill marker is retained.
    Signal quantities are pre-headroom targets, rather than accepted order sizes.
    """
    directory, output = Path(directory), Path(output)
    required = ('parameters.json', 'history.csv', 'orders.csv', 'signals.csv', 'metrics.json')
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f'No completed frozen backtest at {directory}.\n'
            f'Missing files: {", ".join(missing)}.\n'
            'Generate the results first: python research/run_frozen_backtest.py\n'
            'Then redraw: python research/plot_frozen_backtest.py'
        )

    import matplotlib
    matplotlib.use('Agg')  # PNG-only rendering; do not initialize a macOS GUI.
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import Patch

    settings = json.loads((directory / 'parameters.json').read_text())
    tick = settings['tick_size']
    interval = _sample_interval_ns(sample_interval_seconds)
    rows = _read_plot_rows(directory / 'history.csv', ('timestamp_ns', 'instrument_price',
                          'position_size', 'equity', 'average_entry_price'), interval)
    timed = [int(row['timestamp_ns']) for row in rows if row['timestamp_ns']]
    if not timed:
        raise ValueError('History has no timestamped observations')
    start, end = timed[0], timed[-1]
    hours = [((int(row['timestamp_ns']) if row['timestamp_ns'] else start) - start) / HOUR_NS
             for row in rows]

    def values(name):
        return [float(row[name]) if row.get(name) else float('nan') for row in rows]

    prices = values('instrument_price')
    signals = _read_plot_rows(directory / 'signals.csv', ('timestamp_ns', 'center_price'), interval)
    signal_hours = [(int(row['timestamp_ns']) - start) / HOUR_NS for row in signals]

    def signal_values(name):
        return [float(row[name]) if row.get(name) else float('nan') for row in signals]

    records = list(load_order_history(directory / 'orders.csv'))
    resting_prices, remaining = _resting_segments(records, start, end, sample_interval_ns=interval)
    with plt.style.context('classic'), plt.rc_context({'font.family': 'monospace',
                                                      'font.monospace': ['DejaVu Sans Mono']}):
        figure, axes = plt.subplots(12, 1, sharex=True, figsize=(15, 32))
        position, equity, pnl, costs, price, absolute, relative, size, width, skew, trend, rv = axes
        quote = position.twinx()
        position.step(hours, values('position_size'), where='post', color='b', label='Base position')
        quote.plot(hours, values('position_balance'), color='#cc8800', label='Quote position')
        quote.set_ylabel('Position (USD)')
        position.set_ylabel('Position (ETH)')
        position.legend(handles=[position.lines[0], quote.lines[0]], fontsize=8)
        equity.plot(hours, values('equity'), color='g', label='Equity')
        equity.set_ylabel('USD')
        for name, label in (('total_pnl', 'Total'), ('net_realized_pnl', 'Net realized'),
                            ('unrealized_pnl', 'Unrealized')):
            pnl.plot(hours, values(name), label=label)
        pnl.set_ylabel('PnL (USD)')
        for name, label in (('trading_fees', 'Fees paid'), ('funding_pnl', 'Settled funding PnL')):
            costs.step(hours, values(name), where='post', label=label)
        costs.set_ylabel('USD')
        price.plot(hours, prices, label='Mid / mark', color='k')
        entry = [float(row['average_entry_price']) if float(row['position_size']) != 0
                 and row.get('average_entry_price') else float('nan') for row in rows]
        price.plot(hours, entry, label='Average entry', color='#cc8800')
        price.set_ylabel('USD')
        for side, color in ((Side.BUY, 'g'), (Side.SELL, 'r')):
            absolute.add_collection(LineCollection(resting_prices[side], colors=color,
                                                   linewidths=0.7, label=f'{side.value} limits'))
            size.add_collection(LineCollection(remaining[side], colors=color,
                                               linewidths=0.7, label=f'{side.value} remaining'))
            placements = list(_sample_by_time((row for row in records if row.event == 'place'
                                               and row.side == side), lambda row: row.timestamp_ns, interval))
            offsets = [(row.timestamp_ns, (row.price - row.instrument_price) / tick)
                       for row in placements if row.instrument_price is not None]
            relative.scatter([(timestamp - start) / HOUR_NS for timestamp, _ in offsets],
                             [value for _, value in offsets], s=4, color=color,
                             alpha=0.4, label=f'{side.value} placements')
            book_field = 'bid_price_1' if side == Side.BUY else 'ask_price_1'
            relative.plot(hours, [(float(row[book_field]) - float(row['instrument_price'])) / tick
                                 if row.get(book_field) and row.get('instrument_price') else float('nan')
                                 for row in rows], color=color, alpha=0.3, label=book_field)
        fills = [row for row in records if row.event == 'fill']
        absolute.scatter([(row.timestamp_ns - start) / HOUR_NS for row in fills],
                         [row.fill_price for row in fills], marker='x', s=8, color='k', label='Fills')
        relative.axhline(0, color='k', linewidth=0.6)
        relative.autoscale_view()
        extent = max(abs(value) for value in relative.get_ylim())
        relative.set_ylim(-extent, extent)
        absolute.set_ylabel('Order price (USD)')
        relative.set_ylabel('Price - mid (ticks)')
        size.set_ylabel('Remaining (ETH)')
        width.plot(signal_hours, signal_values('placement_width_ticks'), label='Full target width')
        width.set_ylabel('Ticks')
        skew.plot(signal_hours, [(float(row['center_price']) - float(row['instrument_price'])) / tick
                                if row.get('center_price') and row.get('instrument_price') else float('nan')
                                for row in signals], label='Trend center shift')
        skew.set_ylabel('Ticks')
        trend.plot(signal_hours, signal_values('trend_magnitude'), label='Trend magnitude')
        trend.set_ylabel('Normalized trend')
        rv.plot(signal_hours, [100 * value for value in signal_values('annualized_volatility')],
                label='Realized volatility')
        rv.set_ylabel('% p.a.')
        titles = ('Base / quote position', 'Equity', 'Realized / unrealized PnL',
                  'Fees and funding', 'Price and average entry', 'Resting orders and fills',
                  'Placements relative to midpoint', 'Resting order quantities',
                  'Quote width', 'Quote-center skew (trend only)', 'Local trend', 'Realized volatility')
        for axis, title in zip(axes, titles):
            axis.set_title(title, fontsize=11)
            axis.autoscale_view()
            axis.grid(alpha=0.2)
            axis.legend(fontsize=8, loc='upper left')
            _add_price_overlay(axis, hours, prices, right_offset=65 if axis is position else 0)
            for _, begin, finish, color in REGIONS:
                left, right = max(start, utc_ns(begin)), min(end, utc_ns(finish))
                if left < right:
                    axis.axvspan((left - start) / HOUR_NS, (right - start) / HOUR_NS,
                                 color=color, alpha=0.15, zorder=-1)
            axis.set_xlim(0, (end - start) / HOUR_NS)
        rv.set_xlabel('Elapsed hours (UTC start: ' + datetime.fromtimestamp(
            start // 1_000_000_000, timezone.utc).isoformat() + ')')
        figure.suptitle('Frozen parameters: continuous train / validation / OOS backtest', fontsize=14)
        figure.legend(handles=[Patch(facecolor=color, alpha=0.3, label=label)
                               for label, _, _, color in REGIONS], loc='upper center',
                      bbox_to_anchor=(0.5, 0.987), ncol=3, fontsize=10)
        figure.tight_layout(rect=(0, 0, 1, 0.975), h_pad=1.2)
        output.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(output, dpi=150)
        plt.close(figure)
    return output


if __name__ == '__main__':
    try:
        print(plot_frozen_backtest())
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from None
