"""Coarse-to-fine PnL search; run manually: python research/grid_neutral_staged.py.

Edit CONFIG below. All stages use the same data window, strategy and accounting
as the research strategy. No backtests run on import. Histories are saved by the
research runner; stage summaries may reference reused runs from an earlier stage.
"""

import csv
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from itertools import product
from math import exp, isfinite, log
from pathlib import Path
from typing import Any, Literal

if __package__:
    from .grid_runner import FIXED_PARAMETERS, N_HOURS, START_DATE, WORKERS, NeutralTrendGateStrategy, run_case
else:
    from grid_runner import FIXED_PARAMETERS, N_HOURS, START_DATE, WORKERS, NeutralTrendGateStrategy, run_case


@dataclass(frozen=True)
class Axis:
    """Global parameter bounds; log spacing means geometric values and steps."""

    lower: float
    upper: float
    scale: Literal['linear', 'log'] = 'linear'

    def __post_init__(self) -> None:
        if not isfinite(self.lower) or not isfinite(self.upper) or self.lower >= self.upper:
            raise ValueError('Axis bounds must be finite and increasing')
        if self.scale not in ('linear', 'log'):
            raise ValueError('Axis scale must be linear or log')
        if self.scale == 'log' and self.lower <= 0:
            raise ValueError('Log axis bounds must be positive')

    def coordinate(self, value: float) -> float:
        return log(value) if self.scale == 'log' else value

    def value(self, coordinate: float) -> float:
        value = exp(coordinate) if self.scale == 'log' else coordinate
        return min(self.upper, max(self.lower, value))


ROOT = Path(__file__).resolve().parents[1]
CONFIG = dict(
    data_directory=ROOT / 'data',
    output_directory=ROOT / 'figures',
    start_date=START_DATE,
    hours=N_HOURS,
    workers=WORKERS,  # Set to 1 for serial runs.
    first_stage_min_turnover_24h=1.0,  # Traded notional / allocation, per 24 hours.
    fixed_parameters=dict(FIXED_PARAMETERS),  # Includes 50% reference RV and 1h warm-up.
    axes={
        'half_spread_ticks_at_target': Axis(25.0, 250.0),
        'half_life_seconds': Axis(15 * 60.0, 3 * 3600.0, scale='log'),
        'trend_skew_strength': Axis(0.0, 1.0),
    },
    stages=[
        dict(points=5),
        dict(points=7, radius_fraction=0.5),
        # dict(points=5, radius_fraction=0.5),  # Optional third stage.
    ],
)
# points can also be a dict with one count per axis, e.g.:
# {'half_spread_ticks_at_target': 5, 'half_life_seconds': 7, 'trend_skew_strength': 3}
# Refinement counts must be odd and >= 3, so the previous best stays in the grid.
# radius_fraction <= 0.5 keeps each side inside half the previous neighboring step.


def point_counts(spec: int | dict[str, int], names, *, refinement: bool) -> dict[str, int]:
    counts = dict(spec) if isinstance(spec, dict) else dict.fromkeys(names, spec)
    if set(counts) != set(names):
        raise ValueError('Point counts must specify every axis exactly once')
    for count in counts.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 2:
            raise ValueError('Point counts must be integers >= 2')
        if refinement and (count < 3 or count % 2 == 0):
            raise ValueError('Refinement counts must be odd integers >= 3')
    return counts


def sample_axis(axis: Axis, lower: float, upper: float, count: int) -> list[float]:
    lo, hi = axis.coordinate(lower), axis.coordinate(upper)
    values = [axis.value(lo + (hi - lo) * i / (count - 1)) for i in range(count)]
    # Exact endpoints/centers let duplicate runs be reused despite exp/log rounding.
    values[0], values[-1] = lower, upper
    return sorted(set(values))


def initial_grid(axes: dict[str, Axis], counts: dict[str, int]) -> dict[str, list[float]]:
    return {name: sample_axis(axis, axis.lower, axis.upper, counts[name])
            for name, axis in axes.items()}


def refine_grid(axes: dict[str, Axis], previous: dict[str, list[float]],
                best: dict, counts: dict[str, int], radius_fraction: float = 0.5) -> dict[str, list[float]]:
    """Refine around the best using half-neighbor steps in each axis's coordinates.

    Linear axes use additive distances; log axes use log-ratios. At domain edges
    clip bounds and collapse repeated points, retaining the exact best point.
    Nonuniform preceding grids use separate left/right neighboring distances.
    """
    if not isfinite(radius_fraction) or not 0 < radius_fraction <= 0.5:
        raise ValueError('Refinement radius_fraction must be in (0, 0.5]')
    refined = {}
    for name, axis in axes.items():
        values, center = previous[name], best[name]
        index = values.index(center)
        coordinates = [axis.coordinate(value) for value in values]
        c = coordinates[index]
        if len(values) == 1:
            refined[name] = [center]
            continue
        left_step = c - coordinates[index - 1] if index else coordinates[1] - c
        right_step = coordinates[index + 1] - c if index < len(values) - 1 else left_step
        lower = axis.value(c - radius_fraction * left_step)
        upper = axis.value(c + radius_fraction * right_step)
        # Splitting at center preserves it even when a bound is clipped.
        half_count = counts[name] // 2 + 1
        refined[name] = sorted(set(sample_axis(axis, lower, center, half_count)
                                   + sample_axis(axis, center, upper, half_count)))
    return refined


def parameter_grid(values: dict[str, list[float]]) -> list[dict[str, float]]:
    return [dict(zip(values, combination)) for combination in product(*values.values())]


def best_by_pnl(rows: list[dict]) -> dict:
    if not rows or any(not isfinite(row['total_pnl_usd']) for row in rows):
        raise ValueError('Each completed run must have a finite total_pnl_usd')
    # Deterministic ties: prefer the earliest run, independent of worker completion.
    return max(rows, key=lambda row: (row['total_pnl_usd'], -row['run_id']))


def turnover_24h(row: dict) -> float:
    """Normalize traded notional / initial allocation using actual window hours.

    The runner's turnover already divides by allocation. Include warm-up in the
    elapsed period; shorter/trimmed windows are normalized to a 24-hour rate.
    """
    turnover, hours = row['turnover'], row['effective_hours']
    if not isfinite(turnover) or turnover < 0 or not isfinite(hours) or hours <= 0:
        raise ValueError('Turnover must be finite/nonnegative and effective_hours finite/positive')
    return turnover * (24 / hours)  # A 24h window preserves the reported ratio exactly.


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + '\n', encoding='utf-8')


def checkpoint(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: row['run_id']))
    temporary.replace(path)


def run_search(config: dict[str, Any]) -> Path:
    """Run two/three sequential stages; parallelize independent cases within stages."""
    axes, stages = config['axes'], config['stages']
    workers, hours = config['workers'], config['hours']
    min_turnover = config.get('first_stage_min_turnover_24h', 1.0)
    if not isfinite(min_turnover) or min_turnover < 0:
        raise ValueError('first_stage_min_turnover_24h must be finite and nonnegative')
    if not axes or len(stages) not in (2, 3):
        raise ValueError('Configure at least one axis and two or three stages')
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError('workers must be a positive integer')
    if not isfinite(hours) or hours <= 0:
        raise ValueError('hours must be finite and positive')
    date.fromisoformat(config['start_date'])
    counts = [point_counts(stage['points'], axes, refinement=i > 0) for i, stage in enumerate(stages)]
    for stage in stages[1:]:
        fraction = stage.get('radius_fraction', 0.5)
        if not isfinite(fraction) or not 0 < fraction <= 0.5:
            raise ValueError('Refinement radius_fraction must be in (0, 0.5]')
    fixed = {name: value for name, value in FIXED_PARAMETERS.items() if name not in axes}
    fixed.update(config['fixed_parameters'])
    if set(axes) & set(fixed):
        raise ValueError('A parameter cannot be both fixed and searched; remove it from fixed_parameters')
    # Validate strategy names/settings before creating directories or starting work.
    NeutralTrendGateStrategy(**fixed, **{name: axis.lower for name, axis in axes.items()})
    values = initial_grid(axes, counts[0])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = Path(config['output_directory']) / f'neutral_staged_grid_{config["start_date"]}_{hours:g}h_{stamp}'
    output.mkdir(parents=True, exist_ok=False)
    save_json(output / 'config.json', {**config, 'fixed_parameters': fixed,
                                     'first_stage_min_turnover_24h': min_turnover,
                                     'axes': {name: asdict(axis) for name, axis in axes.items()},
                                     'objective': 'total_pnl_usd'})
    print(f'Output: {output}', flush=True)
    cache, completed = {}, []
    previous_best = None
    overall_best = None
    next_run_id = 1
    with ExitStack() as stack:
        pool = stack.enter_context(ProcessPoolExecutor(max_workers=workers)) if workers > 1 else None
        for stage_number, stage in enumerate(stages, 1):
            if previous_best is not None:
                values = refine_grid(axes, values, previous_best, counts[stage_number - 1],
                                     stage.get('radius_fraction', 0.5))
            grid = parameter_grid(values)
            stage_directory = output / f'stage_{stage_number:02d}'
            stage_directory.mkdir()
            save_json(stage_directory / 'grid.json', dict(values=values, settings=stage,
                      previous_best=previous_best, n_points=len(grid),
                      min_turnover_24h=min_turnover if stage_number == 1 else None))
            stage_rows, pending = [], []

            def stage_result(row, *, reused):
                return dict(stage=stage_number, reused=reused,
                            eligible_for_selection=stage_number > 1 or row['turnover_24h'] >= min_turnover,
                            **row)

            def consider_best(row):
                nonlocal overall_best
                if row['eligible_for_selection']:
                    overall_best = best_by_pnl([overall_best, row] if overall_best is not None else [row])

            for parameters in grid:
                key = tuple(parameters[name] for name in axes)
                if key in cache:
                    result = stage_result(cache[key], reused=True)
                    stage_rows.append(result)
                    consider_best(result)
                else:
                    pending.append((key, parameters, next_run_id))
                    next_run_id += 1
            print(f'Stage {stage_number}/{len(stages)}: {len(grid)} points, '
                  f'{len(pending)} new runs, {len(stage_rows)} reused', flush=True)
            checkpoint(stage_directory / 'summary.csv', stage_rows)

            def collect(key, row):
                best_by_pnl([row])  # Reject unusable objective values immediately.
                row = dict(row, turnover_24h=turnover_24h(row))
                cache[key] = row
                result = stage_result(row, reused=False)
                completed.append(dict(first_stage=stage_number,
                                      first_stage_turnover_eligible=(result['eligible_for_selection']
                                                                     if stage_number == 1 else None), **row))
                stage_rows.append(result)
                consider_best(result)
                checkpoint(output / 'summary.csv', completed)
                checkpoint(stage_directory / 'summary.csv', stage_rows)
                save_json(output / 'best.json', overall_best)
                print(f'Stage {stage_number}: {len(stage_rows)}/{len(grid)}, '
                      f'run_{row["run_id"]:03d}, PnL=${row["total_pnl_usd"]:.4f}', flush=True)

            kwargs = dict(data=Path(config['data_directory']), output=output,
                          day=config['start_date'], hours=hours, fixed_parameters=fixed)
            if pool is None:
                for key, parameters, run_id in pending:
                    collect(key, run_case(parameters, run_id=run_id, **kwargs))
            else:
                futures = {pool.submit(run_case, parameters, run_id=run_id, **kwargs): key
                           for key, parameters, run_id in pending}
                for future in as_completed(futures):
                    collect(futures[future], future.result())
            eligible_rows = [row for row in stage_rows if row['eligible_for_selection']]
            print(f'Stage {stage_number}: {len(eligible_rows)}/{len(stage_rows)} eligible for selection', flush=True)
            if not eligible_rows:
                save_json(stage_directory / 'best.json', None)
                raise ValueError(f'Stage {stage_number}: no points meet the minimum 24h turnover '
                                 f'of {min_turnover:g}x allocation; reports saved in {output}')
            previous_best = best_by_pnl(eligible_rows)
            save_json(stage_directory / 'best.json', previous_best)
            save_json(output / 'best.json', overall_best)
            print(f'Stage {stage_number} best PnL=${previous_best["total_pnl_usd"]:.4f}: '
                  f'{ {name: previous_best[name] for name in axes} }', flush=True)
    print(f'Saved {output / "summary.csv"} and {output / "best.json"}', flush=True)
    return output


def main() -> None:
    run_search(CONFIG)


if __name__ == '__main__':
    main()
