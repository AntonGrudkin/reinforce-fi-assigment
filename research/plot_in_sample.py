"""Plot saved fill CSVs: python research/plot_in_sample.py [run_directory].

PnL plots use only decreasing executions; width/activity uses all executions.
Volatility bands pool the configured widths
and weight every execution equally, including partial fills. They describe
gross closing PnL, rather than net strategy performance or a causal trend effect.
"""

import csv
import json
import sys
from dataclasses import dataclass
from math import floor, isfinite
from pathlib import Path


N_BINS = 10
FULL_WIDTH_FILTER: tuple[float, ...] | None = None  # e.g. (100, 150, 200, 300, 400)
SCATTER_ALPHA = 0.1


@dataclass(frozen=True)
class ClosingFill:
    volatility_percent: float
    order_return_percent: float
    realized_pnl: float
    signed_trend: float | None
    side: str = ''


def read_closing_fills(path: str | Path, widths: tuple[float, ...] | None = None) -> list[ClosingFill]:
    """Read decreasing fills; retain break-even closes and skip missing features."""
    records = []
    required = {'position_effect', 'side', 'quantity', 'fill_price',
                'realized_pnl_from_fill', 'annualized_volatility', 'trend_magnitude'}
    with Path(path).open(newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        missing = required - set(reader.fieldnames or ())
        if widths is not None and 'full_width_ticks' not in (reader.fieldnames or ()):
            missing.add('full_width_ticks')
        if missing:
            raise ValueError(f'{path}: missing columns {sorted(missing)}')
        for row in reader:
            if row['position_effect'] != 'decrease':
                continue
            if widths is not None and float(row['full_width_ticks']) not in widths:
                continue
            if not row['realized_pnl_from_fill'] or not row['annualized_volatility']:
                continue
            pnl, rv = float(row['realized_pnl_from_fill']), float(row['annualized_volatility'])
            quantity, price = float(row['quantity']), float(row['fill_price'])
            if not all(isfinite(value) for value in (pnl, rv, quantity, price)):
                continue
            if rv < 0 or quantity <= 0 or price <= 0:
                continue
            if row['side'] not in ('buy', 'sell'):
                raise ValueError(f'{path}: unknown fill side {row["side"]!r}')
            trend = float(row['trend_magnitude']) if row['trend_magnitude'] else None
            signed = None
            if trend is not None and isfinite(trend):
                signed = (-1 if row['side'] == 'buy' else 1) * trend
            records.append(ClosingFill(100 * rv, 100 * pnl / (quantity * price), pnl, signed, row['side']))
    return records


def shared_bin_edges(groups: list[list[ClosingFill]], n_bins: int) -> list[float]:
    """Use one equal-width range for both modes; the final bin includes its edge."""
    if type(n_bins) is not int or n_bins <= 0:
        raise ValueError('n_bins must be a positive integer')
    values = [row.volatility_percent for group in groups for row in group]
    if not values:
        raise ValueError('No decreasing fills with observed RV and realized PnL')
    lower, upper = min(values), max(values)
    if lower == upper:
        lower, upper = max(0.0, lower - 0.5), upper + 0.5
    return [lower + (upper - lower) * i / n_bins for i in range(n_bins + 1)]


def _quantile(ordered: list[float], fraction: float) -> float:
    """Linear interpolation, matching pandas' default quantile convention."""
    offset = (len(ordered) - 1) * fraction
    left = floor(offset)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (offset - left)


def binned_returns(records: list[ClosingFill], edges: list[float]) -> list[tuple[float, float, float]]:
    groups = [[] for _ in edges[:-1]]
    width = (edges[-1] - edges[0]) / len(groups)
    for row in records:
        index = min(len(groups) - 1, max(0, floor((row.volatility_percent - edges[0]) / width)))
        groups[index].append(row.order_return_percent)
    # Keep gaps for empty bins rather than joining across unobserved regions.
    return [tuple(_quantile(sorted(values), q) for q in (0.25, 0.5, 0.75))
            if values else (float('nan'),) * 3 for values in groups]


def read_width_activity(
    path: str | Path, widths: tuple[float, ...] | None = None,
    configured_widths: tuple[float, ...] = (),
) -> dict[float, tuple[int, float]]:
    """Count all executions (including partial/increasing fills) and USD notional.

    Seed configured widths so zero-fill cases remain visible. Do not require
    realized PnL, RV or trend: missing signal values do not remove traded volume.
    """
    activity = {float(width): (0, 0.0) for width in configured_widths
                if widths is None or width in widths}
    with Path(path).open(newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        missing = {'full_width_ticks', 'quantity', 'fill_price'} - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f'{path}: missing columns {sorted(missing)}')
        for row in reader:
            if row.get('adjust_width_to_volatility', '').strip().lower() in ('true', '1'):
                raise ValueError('Width/activity plot requires volatility adjustment off')
            width, quantity, price = (float(row[name])
                                      for name in ('full_width_ticks', 'quantity', 'fill_price'))
            if not all(isfinite(value) and value > 0 for value in (width, quantity, price)):
                raise ValueError(f'{path}: invalid width, quantity or fill price')
            if widths is not None and width not in widths:
                continue
            count, notional = activity.get(width, (0, 0.0))
            activity[width] = (count + 1, notional + quantity * price)
    return dict(sorted(activity.items()))


def plot_width_activity(
    csv_path: str | Path, png_path: str | Path, *,
    widths: tuple[float, ...] | None = FULL_WIDTH_FILTER,
    configured_widths: tuple[float, ...] = (),
) -> Path:
    """Plot existing off-mode fills independently of the two PnL charts."""
    from matplotlib import rc_context, style
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    activity = read_width_activity(csv_path, widths, configured_widths)
    if not activity:
        raise ValueError('No quote widths available for the width/activity plot')
    with style.context('classic'), rc_context({'font.family': 'monospace',
                                              'font.monospace': ['DejaVu Sans Mono'],
                                              'figure.facecolor': 'white'}):
        figure = Figure(figsize=(8, 7), layout='constrained')
        FigureCanvasAgg(figure)
        counts, volume = figure.subplots(2, 1, sharex=True)
        x = list(activity)
        counts.plot(x, [value[0] for value in activity.values()], 'bo-')
        counts.set(ylabel='Fill count', title='Quote width vs trading activity (vol adjustment off)')
        volume.plot(x, [value[1] for value in activity.values()], 'ro-')
        volume.set(xlabel='Full quote width (ticks)', ylabel='Traded notional (USD)')
        for axis in (counts, volume):
            axis.set_ylim(bottom=0)
            axis.ticklabel_format(axis='y', style='plain', useOffset=False)
            axis.grid(alpha=0.2)
        volume.set_xticks(x)
        path = Path(png_path)
        figure.savefig(path, dpi=150)
        figure.clear()
    return path


def plot_results(
    directory: str | Path, *, n_bins: int = N_BINS,
    widths: tuple[float, ...] | None = FULL_WIDTH_FILTER,
) -> tuple[Path, Path, Path]:
    """Plot CSVs only; never import or invoke the backtest pipeline."""
    from matplotlib import rc_context, style
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    directory = Path(directory)
    without = read_closing_fills(directory / 'without_vol_adjustment' / 'fills.csv', widths)
    with_adjustment = read_closing_fills(directory / 'with_vol_adjustment' / 'fills.csv', widths)
    edges = shared_bin_edges([without, with_adjustment], n_bins)
    centers = [(left + right) / 2 for left, right in zip(edges, edges[1:])]
    volatility_path, trend_path = directory / 'pnl_vs_volatility.png', directory / 'pnl_vs_signed_trend.png'
    with style.context('classic'), rc_context({'font.family': 'monospace',
                                              'font.monospace': ['DejaVu Sans Mono'],
                                              'figure.facecolor': 'white'}):
        figure = Figure(figsize=(8, 5), layout='constrained')
        FigureCanvasAgg(figure)
        axis = figure.subplots()
        for records, label, color in ((without, 'without vol adj', 'b'),
                                      (with_adjustment, 'with vol adj', 'r')):
            bands = binned_returns(records, edges)
            q25, q50, q75 = ([row[i] for row in bands] for i in range(3))
            axis.fill_between(centers, q25, q75, color=color, alpha=0.25)
            axis.plot(centers, q50, color=color, label=f'{label} (n={len(records)})')
        axis.set(xlabel='Observed volatility (% p.a.)',
                 ylabel='Realized PnL / filled notional (%)',
                 title='Decreasing fills: median and 25–75% band')
        axis.legend(loc='best', fontsize=9)
        figure.savefig(volatility_path, dpi=150)
        figure.clear()

        figure = Figure(figsize=(8, 5), layout='constrained')
        FigureCanvasAgg(figure)
        axis = figure.subplots()
        trend_rows = [row for row in without if row.signed_trend is not None]
        points = axis.scatter([row.signed_trend for row in trend_rows],
                              [row.realized_pnl for row in trend_rows],
                              c=[row.volatility_percent for row in trend_rows],
                              alpha=SCATTER_ALPHA, cmap='viridis', s=20)
        colorbar = figure.colorbar(points, ax=axis, label='Observed volatility (% p.a.)')
        # Scatter transparency should not fade the color scale itself.
        if colorbar.solids is not None:
            colorbar.solids.set_alpha(1)
        if not trend_rows:
            axis.text(0.5, 0.5, 'No decreasing fills with an observed trend',
                      transform=axis.transAxes, ha='center', fontsize=9)
        axis.set(xlabel='Signed trend (−trend for buy, +trend for sell)',
                 ylabel='Realized PnL from fill (USD)',
                 title=f'Decreasing fills without vol adjustment (n={len(trend_rows)})')
        figure.savefig(trend_path, dpi=150)
        figure.clear()
    metadata_path = directory / 'metadata.json'
    configured = (tuple(json.loads(metadata_path.read_text(encoding='utf-8'))['full_widths_ticks'])
                  if metadata_path.is_file() else ())
    activity_path = plot_width_activity(directory / 'without_vol_adjustment' / 'fills.csv',
                                        directory / 'quote_width_vs_activity.png',
                                        widths=widths, configured_widths=configured)
    return volatility_path, trend_path, activity_path


def main(directory: str | Path | None = None) -> tuple[Path, Path, Path]:
    if directory is None:
        figures = Path(__file__).resolve().parents[1] / 'figures'
        completed = []
        for candidate in sorted(figures.glob('in_sample_*')):
            metadata_path = candidate / 'metadata.json'
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                if metadata.get('status') == 'complete' and set(metadata['volatility_adjustments']) == {False, True}:
                    completed.append(candidate)
        if not completed:
            raise ValueError('No completed two-mode in-sample run; supply a CSV run directory')
        directory = completed[-1]
    outputs = plot_results(directory)
    for path in outputs:
        print(f'Saved: {path}')
    return outputs


if __name__ == '__main__':
    if len(sys.argv) > 2:
        raise SystemExit('Usage: python research/plot_in_sample.py [run_directory]')
    main(sys.argv[1] if len(sys.argv) == 2 else None)
