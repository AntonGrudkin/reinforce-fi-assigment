"""Static README figures from saved CSVs; never run a backtest or grid search.

Run: python research/plot_research_findings.py. Change the two source directories
below to redraw against another saved experiment. Outputs overwrite static PNGs.
"""

import csv
from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path

if __package__:
    from .plot_in_sample import N_BINS, SCATTER_ALPHA, binned_returns, read_closing_fills, shared_bin_edges
    from .freeze_stage_two import analyze_cells, load_cells, save_result
else:
    from plot_in_sample import N_BINS, SCATTER_ALPHA, binned_returns, read_closing_fills, shared_bin_edges
    from freeze_stage_two import analyze_cells, load_cells, save_result


ROOT = Path(__file__).resolve().parents[1]
IN_SAMPLE_DIRECTORY = ROOT / 'figures' / 'in_sample_2026-03-19_20261005T171843768218Z'
GRID_DIRECTORY = ROOT / 'figures' / 'neutral_staged_grid_2026-03-19_24h_20261005T185535437454Z'
OUTPUT_DIRECTORY = ROOT / 'research' / 'plots'
PARAMETERS = ('half_spread_ticks_at_target', 'half_life_seconds', 'trend_skew_strength')
LABELS = dict(half_spread_ticks_at_target='Half quote width (ticks)',
              half_life_seconds='Half-life (hours)', trend_skew_strength='Trend skew strength')


@dataclass(frozen=True)
class GridPoint:
    run_id: int
    parameters: dict[str, float]
    pnl: float
    eligible: bool


def read_grid(path: Path) -> list[GridPoint]:
    with path.open(newline='', encoding='utf-8') as source:
        points = [GridPoint(int(row['run_id']), {name: float(row[name]) for name in PARAMETERS},
                            float(row['total_pnl_usd']), row['eligible_for_selection'] == 'True')
                  for row in csv.DictReader(source)]
    if not points or any(not isfinite(point.pnl) or
                         not all(isfinite(value) for value in point.parameters.values()) for point in points):
        raise ValueError(f'{path}: expected finite saved grid results')
    if len({tuple(point.parameters[name] for name in PARAMETERS) for point in points}) != len(points):
        raise ValueError(f'{path}: duplicate grid cells')
    return points


def plot_fill_panels(source: Path, output: Path) -> list[Path]:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    without = read_closing_fills(source / 'without_vol_adjustment' / 'fills.csv')
    with_adjustment = read_closing_fills(source / 'with_vol_adjustment' / 'fills.csv')
    # Preserve the original comparison's shared bins while hiding the on-mode curve.
    edges = shared_bin_edges([without, with_adjustment], N_BINS)
    bands = binned_returns(without, edges)
    centers = [(left + right) / 2 for left, right in zip(edges, edges[1:])]
    q25, median, q75 = ([row[i] for row in bands] for i in range(3))
    figure = Figure(figsize=(8, 5), layout='constrained')
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    axis.fill_between(centers, q25, q75, color='b', alpha=.25)
    axis.plot(centers, median, 'b-', label=f'without vol adj (n={len(without)})')
    axis.set(xlabel='Observed volatility (% p.a.)', ylabel='Realized PnL / filled notional (%)',
             title='Decreasing fills: median and 25–75% band')
    axis.legend(loc='best', fontsize=9)
    volatility = output / 'pnl_vs_volatility_without_adjustment.png'
    figure.savefig(volatility, dpi=150)
    adjusted_bands = binned_returns(with_adjustment, edges)
    adjusted_q25, adjusted_median, adjusted_q75 = ([row[i] for row in adjusted_bands] for i in range(3))
    axis.fill_between(centers, adjusted_q25, adjusted_q75, color='r', alpha=.25)
    axis.plot(centers, adjusted_median, 'r-', label=f'with vol adj (n={len(with_adjustment)})')
    axis.legend(loc='best', fontsize=9)
    comparison = output / 'pnl_vs_volatility_comparison.png'
    figure.savefig(comparison, dpi=150)
    figure.clear()

    figure = Figure(figsize=(8, 5), layout='constrained')
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    for side, color in (('sell', 'r'), ('buy', 'g')):
        rows = [row for row in without if row.side == side and row.signed_trend is not None]
        axis.scatter([row.signed_trend for row in rows], [row.realized_pnl for row in rows],
                     color=color, alpha=SCATTER_ALPHA, s=20, edgecolors='none', label=f'{side} (n={len(rows)})')
    axis.set(xlabel='Signed trend (−trend for buy, +trend for sell)',
             ylabel='Realized PnL from fill (USD)', title='Decreasing fills without volatility adjustment')
    legend = axis.legend(loc='best', fontsize=9, scatterpoints=1)
    for handle in legend.legend_handles:
        handle.set_alpha(1)
    trend = output / 'pnl_vs_signed_trend_by_side.png'
    figure.savefig(trend, dpi=150)
    figure.clear()
    return [volatility, comparison, trend]


def plot_grid(points: list[GridPoint], stage: int, output: Path, color_limit: float) -> Path:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import Normalize
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D

    widths = sorted({point.parameters[PARAMETERS[0]] for point in points})
    half_lives = sorted({point.parameters[PARAMETERS[1]] for point in points})
    strengths = sorted({point.parameters[PARAMETERS[2]] for point in points})
    lookup = {tuple(point.parameters[name] for name in PARAMETERS): point for point in points}
    eligible = [point for point in points if point.eligible]
    best = min(eligible, key=lambda point: (-point.pnl, point.run_id)) if eligible else None
    columns = 3 if len(strengths) > 4 else min(2, len(strengths))
    figure = Figure(figsize=(5 * columns, 4 * ceil(len(strengths) / columns)), layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots(ceil(len(strengths) / columns), columns, squeeze=False).ravel()
    active = []
    for axis, strength in zip(axes, strengths):
        matrix = [[lookup[(width, half_life, strength)].pnl
                   if (width, half_life, strength) in lookup else float('nan') for width in widths]
                  for half_life in half_lives]
        artist = axis.imshow(matrix, origin='lower', aspect='auto', interpolation='nearest', cmap='RdYlGn',
                             norm=Normalize(vmin=-color_limit, vmax=color_limit))
        active.append(axis)
        axis.set_xticks(range(len(widths)), [f'{value:.0f}' for value in widths])
        axis.set_yticks(range(len(half_lives)), [f'{value / 3600:.3g}' for value in half_lives])
        axis.tick_params(labelsize=8)
        axis.set(title=f'Trend strength = {strength:.4g}',
                 xlabel=LABELS[PARAMETERS[0]], ylabel=LABELS[PARAMETERS[1]])
        for j, half_life in enumerate(half_lives):
            for i, width in enumerate(widths):
                point = lookup.get((width, half_life, strength))
                if point is not None and not point.eligible:
                    axis.plot(i, j, 'kx', markersize=7, markeredgewidth=1.2)
                if point is not None and point is best:
                    axis.plot(i, j, marker='*', markersize=17, color='white', markeredgecolor='black')
    for axis in axes[len(strengths):]:
        axis.set_visible(False)
    figure.colorbar(artist, ax=active, label='Total net PnL (USD)', shrink=.85)
    status = f'{len(eligible)}/{len(points)} eligible'
    if best is not None:
        status += f'; selected best = ${best.pnl:.2f} (run {best.run_id})'
    figure.suptitle(f'Stage {stage} parameter grid — {status}', fontsize=12)
    handles = [Line2D([], [], marker='*', linestyle='', markersize=12, color='white',
                      markeredgecolor='black', label='Selected best')]
    if len(eligible) < len(points):
        handles.append(Line2D([], [], marker='x', linestyle='', color='black', label='Turnover filter rejected'))
    active[0].legend(handles=handles, loc='upper left', fontsize=7, numpoints=1)
    path = output / f'grid_stage_{stage:02d}.png'
    figure.savefig(path, dpi=150)
    figure.clear()
    return path


def main(in_sample: Path = IN_SAMPLE_DIRECTORY, grid: Path = GRID_DIRECTORY,
         output: Path = OUTPUT_DIRECTORY) -> list[Path]:
    from matplotlib import rc_context, style

    output.mkdir(parents=True, exist_ok=True)
    stages = [read_grid(grid / f'stage_{stage:02d}' / 'summary.csv') for stage in (1, 2)]
    freeze_source = grid / 'stage_02' / 'summary.csv'
    freeze_result = analyze_cells(load_cells(freeze_source))
    limit = max(abs(point.pnl) for points in stages for point in points) or 1
    with style.context('classic'), rc_context({'font.family': 'monospace',
                                              'font.monospace': ['DejaVu Sans Mono'],
                                              'figure.facecolor': 'white'}):
        paths = plot_fill_panels(in_sample, output)
        paths += [plot_grid(points, stage, output, limit) for stage, points in enumerate(stages, 1)]
    _, _, stability_figure = save_result(freeze_result, freeze_source, output)
    paths.append(stability_figure)
    for path in paths:
        print(f'Saved: {path}')
    return paths


if __name__ == '__main__':
    main()
