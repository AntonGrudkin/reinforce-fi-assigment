"""Freeze a stable Stage-2 region from one saved development-results CSV only.

Run: python research/freeze_stage_two.py. No strategy, data reader or backtest
modules are imported. All thresholds are fixed by the documented freeze rule.
"""

import csv
import hashlib
import json
import warnings
from dataclasses import dataclass
from math import ceil, fsum, isfinite, sqrt
from pathlib import Path
from statistics import fmean, pstdev


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'figures' / 'neutral_staged_grid_2026-03-19_24h_20261005T185535437454Z' / 'stage_02' / 'summary.csv'
OUTPUT = ROOT / 'research' / 'plots'
PARAMETERS = ('half_spread_ticks_at_target', 'half_life_seconds', 'trend_skew_strength')
PNL_COLUMN = 'total_pnl_usd'
ELIGIBILITY_COLUMN = 'eligible_for_selection'
SELECTION_STATEMENT = (
    'Final configuration was selected from the stable Stage-2 high-performing '
    'region, not by pointwise maximization of development PnL.'
)


@dataclass(frozen=True)
class GridCell:
    run_id: int
    parameters: dict[str, float]
    net_pnl: float
    eligible: bool


@dataclass
class FreezeResult:
    report: dict
    audit: list[dict]


def load_cells(path: Path) -> list[GridCell]:
    """Require explicit Stage-2 eligibility; never recompute a turnover filter."""
    with path.open(newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        required = {'stage', 'run_id', PNL_COLUMN, ELIGIBILITY_COLUMN, *PARAMETERS}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f'Missing Stage-2 columns: {sorted(missing)}')
        cells = []
        for row in reader:
            if int(row['stage']) != 2:
                raise ValueError('Freeze input must contain Stage-2 rows only')
            # The known development window starts March 19; its midnight endpoint
            # can be March 20, but later-day runs must never enter this procedure.
            if row.get('window_start_utc') and not row['window_start_utc'].startswith('2026-03-19T'):
                raise ValueError('Freeze input must use the March 19 development sample')
            if row.get('window_end_ns') and int(row['window_end_ns']) > 1773964800000000000:
                raise ValueError('Freeze input must end by March 20 00:00 UTC; later-day results are excluded')
            flag = row[ELIGIBILITY_COLUMN].strip().lower()
            if flag not in ('true', 'false'):
                raise ValueError('Eligibility must be explicitly True or False')
            parameters = {name: float(row[name]) for name in PARAMETERS}
            pnl = float(row[PNL_COLUMN])
            if not all(isfinite(value) for value in (*parameters.values(), pnl)):
                raise ValueError('Stage-2 parameters and PnL must be finite')
            cells.append(GridCell(int(row['run_id']), parameters, pnl, flag == 'true'))
    if not cells:
        raise ValueError('Stage-2 CSV is empty')
    if len({cell.run_id for cell in cells}) != len(cells):
        raise ValueError('Stage-2 run IDs must be unique')
    if len({tuple(cell.parameters[name] for name in PARAMETERS) for cell in cells}) != len(cells):
        raise ValueError('Stage-2 parameter cells must be unique')
    return cells


def audit_population(cells: list[GridCell]) -> dict:
    eligible = [cell for cell in cells if cell.eligible]
    raw_best = min(eligible, key=lambda cell: (-cell.net_pnl, cell.run_id)) if eligible else None
    return dict(total_rows=len(cells), eligible_rows=len(eligible),
                profitable_eligible_rows=sum(cell.net_pnl > 0 for cell in eligible),
                parameter_columns=PARAMETERS, pnl_column=PNL_COLUMN,
                eligibility_column=ELIGIBILITY_COLUMN,
                raw_maximum=(dict(run_id=raw_best.run_id, parameters=raw_best.parameters,
                                  net_pnl=raw_best.net_pnl) if raw_best else None))


def _stable_suffix_start(projected: list[tuple[float, ...]]) -> int:
    """Return the 1-based start of the last contiguous terminal-cell run."""
    index = len(projected) - 1
    while index > 0 and projected[index - 1] == projected[-1]:
        index -= 1
    return index + 1


def analyze_cells(cells: list[GridCell]) -> FreezeResult:
    """Positive-weight arithmetic centroids; fixed full-eligible standardization."""
    report = audit_population(cells)
    eligible = [cell for cell in cells if cell.eligible]
    profitable = sorted((cell for cell in eligible if cell.net_pnl > 0),
                        key=lambda cell: (-cell.net_pnl, cell.run_id))
    m = len(profitable)
    if m < 10:
        raise ValueError(f'Only {m} profitable eligible configurations; at least 10 are required')
    normalization = {name: dict(mean=fmean(cell.parameters[name] for cell in eligible),
                                std=pstdev(cell.parameters[name] for cell in eligible)) for name in PARAMETERS}
    for name, values in normalization.items():
        if values['std'] == 0:
            raise ValueError(f'Eligible-grid population standard deviation is zero for {name}')
    audit = []
    for n in range(1, m + 1):
        selected = profitable[:n]
        weight = fsum(cell.net_pnl for cell in selected)
        center = {name: fsum(cell.net_pnl * cell.parameters[name] for cell in selected) / weight
                  for name in PARAMETERS}

        def distance_squared(cell):
            return fsum(((cell.parameters[name] - center[name]) / normalization[name]['std']) ** 2
                        for name in PARAMETERS)

        nearest = min(eligible, key=lambda cell: (distance_squared(cell), cell.run_id))
        row = dict(N=n, nth_best_net_pnl=profitable[n - 1].net_pnl, cumulative_weight=weight,
                   projected_run_id=nearest.run_id, projected_net_pnl=nearest.net_pnl,
                   centroid_to_grid_distance=sqrt(distance_squared(nearest)))
        for name in PARAMETERS:
            row[f'centroid_{name}'] = center[name]
            row[f'centroid_z_{name}'] = (center[name] - normalization[name]['mean']) / normalization[name]['std']
            row[f'projected_{name}'] = nearest.parameters[name]
            row[f'projected_z_{name}'] = (nearest.parameters[name] - normalization[name]['mean']) / normalization[name]['std']
        audit.append(row)

    def projected_coordinates(row):
        return tuple(row[f'projected_{name}'] for name in PARAMETERS)

    terminal = audit[-1]
    terminal_coordinates = projected_coordinates(terminal)
    n_freeze = _stable_suffix_start([projected_coordinates(row) for row in audit])
    freeze_index = n_freeze - 1
    stable_length = m - n_freeze + 1
    minimum = max(10, ceil(.10 * m))
    passed = stable_length >= minimum
    for row in audit:
        row['is_terminal_cell'] = projected_coordinates(row) == terminal_coordinates
        row['is_stable_suffix'] = row['N'] >= n_freeze
    freeze_row = audit[freeze_index]
    terminal_config = {name: terminal[f'projected_{name}'] for name in PARAMETERS}
    report.update(
        status='frozen' if passed else 'manual_review_required', M=m,
        normalization=normalization, normalization_ddof=0,
        centroid_units='Original parameter units; half-life in seconds; no logarithmic transform',
        ranking_ties='Lowest run ID', projection_ties='Lowest run ID for equal squared distance',
        profitable_ranked_run_ids=[cell.run_id for cell in profitable],
        eligible_run_ids=sorted(cell.run_id for cell in eligible),
        N_freeze=n_freeze, stable_suffix_length=stable_length, minimum_stable_length=minimum,
        terminal_parameters=terminal_config, terminal_run_id=terminal['projected_run_id'],
        terminal_net_pnl=terminal['projected_net_pnl'],
        final_config=terminal_config if passed else None,
        final_run_id=terminal['projected_run_id'] if passed else None,
        final_net_pnl=terminal['projected_net_pnl'] if passed else None,
        raw_maximum_minus_terminal_pnl=report['raw_maximum']['net_pnl'] - terminal['projected_net_pnl'],
        centroid_at_N_freeze={name: freeze_row[f'centroid_{name}'] for name in PARAMETERS},
        centroid_z_at_N_freeze={name: freeze_row[f'centroid_z_{name}'] for name in PARAMETERS},
        centroid_to_selected_cell_distance=freeze_row['centroid_to_grid_distance'],
        selection_statement=SELECTION_STATEMENT if passed else None,
    )
    return FreezeResult(report, audit)


def plot_stability(result: FreezeResult, output: Path) -> Path:
    from matplotlib import rc_context, style
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator

    with style.context('classic'), rc_context({'font.family': 'monospace',
                                              'font.monospace': ['DejaVu Sans Mono'],
                                              'figure.facecolor': 'white'}):
        figure = Figure(figsize=(10, 12), layout='constrained')
        FigureCanvasAgg(figure)
        axes = figure.subplots(3, 1, sharex=True)
        labels = ('Half quote width (ticks)', 'Half-life (hours)', 'Trend skew strength')
        ns = [row['N'] for row in result.audit]
        for axis, name, label in zip(axes, PARAMETERS, labels):
            unit = 3600 if name == 'half_life_seconds' else 1
            axis.plot(ns, [row[f'centroid_{name}'] / unit for row in result.audit],
                      'b-', label='PnL-weighted centroid')
            axis.step(ns, [row[f'projected_{name}'] / unit for row in result.audit],
                      where='mid', color='r', linestyle='--', label='Nearest eligible grid cell')
            axis.axvline(result.report['N_freeze'], color='black', linestyle=':', label='Freeze point')
            axis.axvspan(result.report['N_freeze'], ns[-1], color='gray', alpha=.10)
            axis.set(ylabel=label, xlim=(1, ns[-1]))
            if name == 'half_spread_ticks_at_target':
                axis.yaxis.set_major_locator(MaxNLocator(integer=True))
            axis.grid(alpha=.2)
        axes[0].legend(loc='best', fontsize=9)
        axes[-1].set_xlabel('Top N profitable eligible points ranked by development net PnL')
        spacing = max(2, ceil(.05 * ns[-1]))
        ticks = [round(value) for value in axes[-1].get_xticks()
                 if 1 + spacing <= value <= ns[-1] - spacing]
        axes[-1].set_xticks(sorted(set([1, ns[-1], *ticks])))
        figure.suptitle('Stability of the high-performing Stage-2 parameter region', fontsize=13)
        caption = ('Profitable eligible development points only; fixed standardized distances.\n'
                   'Freeze selects the stable projected cell, not the raw PnL maximum.'
                   if result.report['status'] == 'frozen' else
                   'Terminal region is insufficiently persistent: manual review required; no configuration frozen.')
        figure.supxlabel(caption, fontsize=9)
        path = output / 'stage_02_top_n_centroids.png'
        figure.savefig(path, dpi=150)
        figure.clear()
    return path


def save_result(result: FreezeResult, source: Path, output: Path) -> tuple[Path, Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    audit_path = output / 'stage_02_parameter_freeze_audit.csv'
    with audit_path.open('w', newline='', encoding='utf-8') as target:
        writer = csv.DictWriter(target, fieldnames=list(result.audit[0]))
        writer.writeheader()
        writer.writerows(result.audit)
    result.report.update(source_csv=str(source.resolve()), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                         audit_csv=str(audit_path.resolve()))
    report_path = output / 'stage_02_parameter_freeze.json'
    report_path.write_text(json.dumps(result.report, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    figure = plot_stability(result, output)
    if result.report['status'] != 'frozen':
        warnings.warn('Centroid projection has no sufficiently persistent stable terminal region; '
                      'manual review required. No configuration frozen.', RuntimeWarning)
    return audit_path, report_path, figure


def main(source: Path = SOURCE, output: Path = OUTPUT) -> FreezeResult:
    cells = load_cells(source)
    population = audit_population(cells)
    print(json.dumps(population, indent=2), flush=True)
    try:
        result = analyze_cells(cells)
    except ValueError as error:
        output.mkdir(parents=True, exist_ok=True)
        population.update(status='error', final_config=None, reason=str(error),
                          source_csv=str(source.resolve()), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        (output / 'stage_02_parameter_freeze.json').write_text(json.dumps(population, indent=2) + '\n', encoding='utf-8')
        raise
    paths = save_result(result, source, output)
    console_keys = ('status', 'M', 'N_freeze', 'stable_suffix_length', 'minimum_stable_length',
                    'final_run_id', 'final_config', 'final_net_pnl', 'raw_maximum',
                    'raw_maximum_minus_terminal_pnl', 'centroid_at_N_freeze',
                    'centroid_to_selected_cell_distance')
    print(json.dumps({key: result.report[key] for key in console_keys}, indent=2), flush=True)
    if result.report['status'] == 'frozen':
        print(SELECTION_STATEMENT, flush=True)
    for path in paths:
        print(f'Saved: {path}', flush=True)
    return result


if __name__ == '__main__':
    main()
