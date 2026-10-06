"""Run the frozen strategy continuously across train, validation and OOS.

Run: python research/run_frozen_backtest.py
Redraw only: python research/plot_frozen_backtest.py
The single warmup occurs at the beginning; inventory/signals carry across splits.
"""

import json
from pathlib import Path

if __package__:
    from .grid_runner import run_case
    from .plot_frozen_backtest import plot_frozen_backtest
else:
    from grid_runner import run_case
    from plot_frozen_backtest import plot_frozen_backtest

ROOT = Path(__file__).resolve().parents[1]
FREEZE_FILE = ROOT / 'research/plots/stage_02_parameter_freeze.json'
OUTPUT = ROOT / 'figures/frozen_backtest'
START_DATE = '2026-03-19'
N_HOURS = 72.0


def frozen_parameters(path: Path = FREEZE_FILE) -> dict:
    """Consume the audited selection, never substitute the raw PnL maximum."""
    freeze = json.loads(path.read_text())
    if freeze.get('status') != 'frozen' or not freeze.get('final_config'):
        raise ValueError('Parameter selection must have a frozen final_config')
    return freeze['final_config']


def main() -> None:
    parameters = frozen_parameters()
    # run_case uses exclusive files/directories: existing results are preserved.
    report = run_case(parameters, data=ROOT / 'data', output=OUTPUT, run_id=1,
                      day=START_DATE, hours=N_HOURS,
                      fixed_parameters=dict(adjust_width_to_volatility=False,
                                            order_fraction=0.01, requote_interval_ms=500.0,
                                            warmup_seconds=3600.0))
    print(json.dumps({key: report[key] for key in (
        'total_pnl_usd', 'n_fills', 'traded_notional_usd', 'max_drawdown_usd',
        'trading_fees_usd', 'funding_pnl_usd', 'effective_hours')}, indent=2), flush=True)
    (OUTPUT / 'freeze_selection.json').write_text(FREEZE_FILE.read_text())
    print(plot_frozen_backtest(OUTPUT / 'run_001', OUTPUT / 'history.png'))


if __name__ == '__main__':
    main()
