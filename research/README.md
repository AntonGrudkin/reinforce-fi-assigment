# In-sample fill analysis

Generate CSVs, then plot them independently:

```sh
python research/in_sample_analysis.py
python research/plot_in_sample.py
# Or select an existing run explicitly:
python research/plot_in_sample.py figures/in_sample_DATE_TIMESTAMP
```

The generator saves everything under a new `figures/in_sample_DATE_TIMESTAMP/`
directory at the project root. It produces `metadata.json`, `summary.csv`, and
`without_vol_adjustment/fills.csv` / `with_vol_adjustment/fills.csv`. The plotting
script adds `pnl_vs_volatility.png`, `pnl_vs_signed_trend.png`, and
`quote_width_vs_activity.png`, using classic
Matplotlib styling and a monospace font. With no argument it selects the latest
completed two-mode run. It never runs a simulation.

Adjust `DAY`, `N_HOURS`, `FULL_WIDTHS_TICKS`, `STRATEGY_PARAMETERS` and
`VOLATILITY_ADJUSTMENTS` in `in_sample_analysis.py`. Defaults preserve the copied
setup: 24 hours from March 19, 300-second half-life, one-hour warm-up, 1% equity
order fraction, 500 ms requoting, trend strength 0.5 and 50% annualized target RV.
Each configured width runs with adjustment off and on; use `(False,)` or `(True,)`
to generate only one mode. Full widths are nominal bid–ask widths: half-width is
`full_width / 2`, multiplied by `RV / target_RV` with a one-tick minimum when
adjustment is enabled. Warm-up stays active in both modes. Windows remain capped
at the available orderbook data. Cash starts at $10,000, maker fees are 0.02%, and
runtime roundtrip latency is 50 ms.

`strategy.py` contains the strategy without unused alpha/microprice controllers.
`signals.py` preserves the original trade-flow signal equations, including
same-timestamp return revision. These files depend only on `market_sim` and the
standard library. CSV generation additionally needs PyArrow and, with progress
enabled, tqdm. Plotting needs Matplotlib; pandas is not required.

Fill CSVs retain every individual partial/full execution. RV and trend are
captured immediately before matching, so the triggering trade is excluded.
Increasing versus decreasing classification compares absolute inventory before
and after the execution. Decreasing fills save the prior average entry and the
gross realized ledger increment, including zero-PnL closes. Reversals realize
only the closed leg. Fees are saved separately; these two plots use gross PnL.

Plot settings are isolated in `plot_in_sample.py`: `N_BINS`, `FULL_WIDTH_FILTER`
and `SCATTER_ALPHA`. Both plots select decreasing fills. Volatility is shown in
percent per annum, using ten shared equal-width bins spanning both datasets.
Median and 25th/75th percentiles use linear interpolation. Empty bins leave gaps.
Widths are pooled unless filtered, and each execution has equal statistical
weight; these are fill distributions, not order-level or notional-weighted
returns. Order return is `100 * realized_pnl_from_fill / (quantity * fill_price)`.
For reversals the denominator remains the entire fill notional, matching the
requested formula.

The trend scatter uses only the adjustment-off dataset:
`x = (-1 for buy, +1 for sell) * trend_magnitude`,
`y = realized_pnl_from_fill` in USD, color = observed RV in % p.a.
Missing RV/PnL values are excluded; missing trend excludes only that scatter
point. The same off-mode CSV is reused, with no additional strategy run.

These are observational in-sample relationships. Existing inventory/trend
controls affect which fills appear; the plots do not identify a causal signal
effect or represent net strategy returns.

The width/activity figure uses only the volatility-adjustment-off CSV and groups
by `full_width_ticks`. It shows fill count and total traded USD notional in two
stacked panels. Every execution counts, including partial fills and both increasing
and decreasing trades; notional is `sum(quantity * fill_price)`. Missing PnL/RV/trend
does not exclude fills. Configured widths with no fills remain visible at zero
when metadata is available. `FULL_WIDTH_FILTER` applies to this chart too.
`plot_width_activity(csv_path, png_path)` can redraw it independently.

## Staged grid search

```sh
python research/grid_neutral_staged.py
```

Edit `CONFIG` in `grid_neutral_staged.py` before running. The copied defaults
use the first 24 hours, a coarse grid with five points per axis, then seven-point
refinement within half the previous neighboring step. Half-life uses geometric
spacing; quote half-width and trend strength use linear spacing. An optional
third stage, separate point counts per axis, parameter bounds, workers and
fixed settings are configurable. `adjust_width_to_volatility` is a fixed boolean
in `CONFIG['fixed_parameters']`; warm-up remains one hour by default.

Selection ranks total net marked PnL. Stage one excludes points with normalized
24h traded notional below one initial allocation; later stages rank by PnL alone.
Duplicate parameter points reuse the earlier run. Set `workers=1` for serial
execution. Importing either grid module does not start simulations.

Results go under `figures/neutral_staged_grid_DATE_HOURS_TIMESTAMP/`. Root and
stage summaries, best points and configuration are saved independently. Each
`run_XXX/` saves parameters/metrics JSON, complete fills/orders CSVs, sampled
accounting/signals CSVs, and 15-minute equity/return CSVs. Drawdown and other risk
metrics use every accounting observation; plot-history sampling does not affect
metrics. Annualized mean and sigma use complete 15-minute simple returns,
365-day scaling and sample standard deviation. EOF-trimmed partial intervals
still affect PnL/drawdown, but are excluded from return statistics.

`grid_runner.py` supplies a single evaluation using the research strategy and
the existing `market_sim` execution/accounting. Its recorded signals omit the
old unused microprice/alpha columns. The search produces data only; no plots or
large jobs are launched automatically during tests.

## Static research figures

Redraw these figures from saved reports without running any simulations:

```sh
python research/plot_research_findings.py
```

Source paths are constants in that script. Fill figures use the completed
in-sample dataset; grid figures use
`figures/neutral_staged_grid_2026-03-19_24h_20261005T185535437454Z/`.
All output names under `research/plots/` are static. Styling is classic
Matplotlib with a monospace font.

### Closing PnL without volatility adjustment

The volatility figure keeps the same shared bins as the original two-mode
comparison, displaying only its adjustment-off curve and 25–75% band. The
signed-trend scatter uses decreasing fills, with sells red and buys green.

![PnL versus volatility without adjustment](plots/pnl_vs_volatility_without_adjustment.png)

The comparison adds adjustment-on results in red; adjustment-off remains blue.
Both series share bin boundaries and show medians with 25–75% bands.

![PnL versus volatility comparison](plots/pnl_vs_volatility_comparison.png)

![PnL versus signed trend by trade side](plots/pnl_vs_signed_trend_by_side.png)

### Coarse and refinement grids

Each panel fixes trend strength and shows saved net PnL versus half quote width
and half-life. Cells show actual evaluated points, without spatial interpolation.
Half-life levels are geometrically spaced and displayed in hours. Both stages use the same color scale;
crosses mark turnover-filtered stage-one points, and a star marks the best
eligible point. Stage one contains 125 points (50 eligible), and stage two
contains 112 points, including its reused coarse-stage center.

![Coarse grid](plots/grid_stage_01.png)

![Refinement grid](plots/grid_stage_02.png)

### Final Stage-2 parameter freeze

```sh
python research/freeze_stage_two.py
```

This deterministic procedure reads only the saved `stage_02/summary.csv` from the
March 19 development grid. It never reads March 20/21 results, changes the grid
or strategy, or starts simulations. The general findings plotter uses this same
procedure when redrawing the final three-panel figure.

Use the existing `eligible_for_selection` flag unchanged. Normalize the original
parameter coordinates once using all eligible Stage-2 rows (including losing
rows), with population standard deviation (`ddof=0`):

```text
z_j(theta) = (theta_j - mean_j) / std_j
```

Only profitable eligible rows (`total_pnl_usd > 0`) enter the centroid population.
Rank them by descending net PnL, with lower run ID breaking ties. For every
`N = 1, ..., M`, calculate an arithmetic positive-PnL-weighted centroid in the
original parameter units, including half-life in seconds:

```text
c_j(N) = sum(PnL_i * theta_ij, i in top N) / sum(PnL_i, i in top N)
g_N = argmin_eligible_cell sum_j ((cell_j - c_j(N)) / std_j)^2
```

Projection candidates are all actual eligible Stage-2 cells, including losing
cells. Equal squared-distance ties choose the lower run ID. No logarithmic
transform, negative centroid weights, PnL shifting or changing normalization is
used. Half-life is converted to hours only for plotting.

Define the terminal cell as `g_M`. Walk backward to find the earliest `N_freeze`
in its final contiguous constant run. Earlier visits to that cell do not count
unless they belong to this terminal suffix. Freeze only if:

```text
M >= 10
stable_suffix_length = M - N_freeze + 1
stable_suffix_length >= max(10, ceil(0.10 * M))
```

Too few profitable rows or a zero parameter standard deviation stops with an
explicit error report. An insufficient stable suffix saves the full audit and
figure, emits a manual-review warning, and leaves `final_config` null. There is
no fallback to the raw PnL maximum and no tuning of `N_freeze` for PnL.

The saved development grid has 112 eligible cells and 103 profitable eligible
cells. The terminal projection stabilizes at **N = 73**, for **31 consecutive
points** (required minimum: 11). The frozen tested cell is **run 199**:

| Parameter | Frozen stable-region cell | Raw maximum (diagnostic only) |
| --- | ---: | ---: |
| Half quote width, ticks | 90.625 | 109.375 |
| Half-life, seconds | 9737.733175591926 | 10800 |
| Half-life, hours | 2.704925882108868 | 3 |
| Trend-skew strength | 0.9583333333333334 | 0.9583333333333334 |
| Development net PnL, USD | 9.614630340256554 | 37.71494100509699 |

The raw maximum is run 235. Its PnL exceeds the selected cell by
$28.100310664840435; it is not used to determine the frozen configuration.
At N = 73, the centroid is `(95.24008242543864, 9313.294762774583,
0.9423432067570197)` in original parameter units. Its standardized distance to
the frozen cell is `0.5780710214471392`.

Final configuration was selected from the stable Stage-2 high-performing
region, not by pointwise maximization of development PnL.

Audit outputs use static names:

- `plots/stage_02_parameter_freeze_audit.csv`: one row per N, including raw and
  standardized centroids/projections, PnL weights, projected PnL/distance and
  terminal/suffix flags.
- `plots/stage_02_parameter_freeze.json`: frozen parameters, comparison, fixed
  normalization, ranked run IDs, thresholds, status, source path and SHA-256.
- `plots/stage_02_top_n_centroids.png`: three vertically aligned panels; blue
  centroids, red projected cells, freeze line and shaded stable suffix.

![Stability of the Stage-2 high-performing region](plots/stage_02_top_n_centroids.png)

## Frozen-parameter backtest

Run the audited `final_config` continuously across March 19 (train), March 20
(validation), and March 21 (OOS), without resetting inventory or signals:

```sh
python research/run_frozen_backtest.py
```

The run uses fixed widths, 1% order fraction, 500 ms requoting, 50 ms latency,
0.02% maker fees, and one initial one-hour warmup. The requested 72-hour window
is trimmed to available book data. Results are saved in
`figures/frozen_backtest/run_001/`; existing run files are protected against
replacement. The selection audit is copied to `figures/frozen_backtest/`.

`figures/frozen_backtest/history.png` combines accounting (excluding execution /
inventory attribution), actual resting orders, centered placements in ticks,
remaining quantities, target width, trend center shift, trend and RV. All panels
share time axes, shaded train / validation / OOS regions and faint price overlays.
CSV exports retain the runner's one-second accounting/signal sampling and full
order/fill lifecycle records. Metrics use every event, before sampling.

Redraw the figure without rerunning the backtest:

```sh
python research/plot_frozen_backtest.py
```

Edit `REGIONS` or plotting code in that file to change presentation. This is a
continuous replay with frozen parameters, so its validation/OOS segments inherit
the preceding position and signal state rather than representing independent
flat-start evaluations.
