"""Single-instrument performance metrics from causal, ordered position history."""

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from math import isclose, sqrt, ulp
from pathlib import Path
from typing import Any

from .history import PositionSnapshot, load_history


DAY_NS = 86_400_000_000_000
YEAR_NS = 365 * DAY_NS
ZERO = 0.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _daily_metrics(
    initial: PositionSnapshot, day_closes: dict[int, PositionSnapshot],
    fill_counts: dict[int, int], start_ns: int, end_ns: int,
) -> list[dict[str, Any]]:
    """Carry last-known equity through gaps; the first/last days may be partial."""
    previous = initial
    daily = []
    for day in range(start_ns // DAY_NS, end_ns // DAY_NS + 1):
        closing = day_closes.get(day, previous)
        pnl = closing.equity - previous.equity
        daily.append({
            "date_utc": datetime.fromtimestamp(day * 86_400, tz=timezone.utc).date().isoformat(),
            "is_partial_day": start_ns > day * DAY_NS or end_ns < (day + 1) * DAY_NS,
            "opening_equity": previous.equity,
            "closing_equity": closing.equity,
            "total_pnl": pnl,
            "return_fraction": _ratio(pnl, previous.equity),
            "realized_pnl": closing.realized_pnl - previous.realized_pnl,
            "unrealized_pnl_change": closing.unrealized_pnl - previous.unrealized_pnl,
            "execution_pnl": closing.execution_pnl - previous.execution_pnl,
            "inventory_pnl": closing.inventory_pnl - previous.inventory_pnl,
            "fees_funding_pnl": closing.fees_funding_pnl - previous.fees_funding_pnl,
            "trading_fees": closing.trading_fees - previous.trading_fees,
            "funding_pnl": closing.funding_pnl - previous.funding_pnl,
            "fill_count": fill_counts.get(day, 0),
        })
        previous = closing
    return daily


def analyze_history(history: Iterable[PositionSnapshot]) -> dict[str, Any]:
    """Compute a report in one pass, retaining only daily closes and filled IDs.

    Require the initial allocation row. Treat each snapshot as a step function
    until the next timestamp; duplicate timestamps have zero weight. Infer fill
    side from the inventory delta, since our CSV has no side column. Daily returns
    use UTC buckets; volatility/Sharpe use only complete days, sample variance,
    zero risk-free rate and sqrt(365) scaling. All ratios are fractions, not %.
    """
    records = iter(history)
    initial = next(records, None)
    if initial is None or initial.event != "initial":
        raise ValueError("History must start with the initial allocation row")
    previous = initial
    start_ns: int | None = None
    end_ns: int | None = None
    count = 1
    day_closes: dict[int, PositionSnapshot] = {}
    daily_fill_counts: dict[int, int] = {}
    filled_ids: set[int] = set()
    buys = sells = settlements = 0
    buy_quantity = sell_quantity = buy_notional = sell_notional = ZERO
    mark_edge = ZERO

    position_integral = abs_position_integral = squared_position_integral = ZERO
    abs_balance_integral = exposure_integral = ZERO
    long_ns = short_ns = positive_equity_ns = 0
    min_position = max_position = initial.position_size
    max_abs_balance = abs(initial.position_balance)
    max_exposure = ZERO if initial.equity > 0 else None
    peak = min_equity = max_equity = initial.equity
    peak_ns: int | None = None
    max_drawdown = ZERO
    max_drawdown_fraction = ZERO if initial.equity > 0 else None
    max_drawdown_duration_ns = 0
    nonpositive_equity = initial.equity <= 0

    for row in records:
        timestamp = row.timestamp_ns
        if timestamp is None:
            raise ValueError("Only the initial allocation row may lack a timestamp")
        if end_ns is not None and timestamp < end_ns:
            raise ValueError("History timestamps must be chronological")
        if start_ns is None:
            start_ns = peak_ns = timestamp
        elapsed = timestamp - end_ns if end_ns is not None else 0
        position_integral += previous.position_size * elapsed
        abs_position_integral += abs(previous.position_size) * elapsed
        squared_position_integral += previous.position_size ** 2 * elapsed
        abs_balance_integral += abs(previous.position_balance) * elapsed
        if previous.position_size > 0:
            long_ns += elapsed
        elif previous.position_size < 0:
            short_ns += elapsed
        if previous.equity > 0:
            positive_equity_ns += elapsed
            exposure_integral += abs(previous.position_balance) / previous.equity * elapsed

        min_position = min(min_position, row.position_size)
        max_position = max(max_position, row.position_size)
        max_abs_balance = max(max_abs_balance, abs(row.position_balance))
        exposure = _ratio(abs(row.position_balance), row.equity)
        if exposure is not None:
            max_exposure = max(max_exposure or ZERO, exposure)
        min_equity = min(min_equity, row.equity)
        max_equity = max(max_equity, row.equity)
        nonpositive_equity |= row.equity <= 0

        # Include the recovery interval when measuring time spent underwater.
        if peak_ns is not None and (previous.equity < peak or row.equity < peak):
            max_drawdown_duration_ns = max(max_drawdown_duration_ns, timestamp - peak_ns)
        if row.equity >= peak:
            peak, peak_ns = row.equity, timestamp
        drawdown = peak - row.equity
        max_drawdown = max(max_drawdown, drawdown)
        fraction = _ratio(drawdown, peak)
        if fraction is not None:
            max_drawdown_fraction = max(max_drawdown_fraction or ZERO, fraction)

        day = timestamp // DAY_NS
        day_closes[day] = row
        if row.event == "FundingSettlement":
            settlements += 1
        if row.event == "fill":
            quantity, price = row.fill_quantity, row.fill_price
            delta = row.position_size - previous.position_size
            tolerance = 4 * max(ulp(row.position_size), ulp(previous.position_size), ulp(quantity or 0.0))
            if quantity is None or price is None or row.order_id is None or quantity <= 0 or price <= 0 or delta == 0 or not isclose(abs(delta), quantity, rel_tol=0, abs_tol=tolerance):
                raise ValueError("Fill row must include price, quantity, order ID, and a matching position change")
            filled_ids.add(row.order_id)
            daily_fill_counts[day] = daily_fill_counts.get(day, 0) + 1
            if delta > 0:
                buys += 1
                buy_quantity += quantity
                buy_notional += price * quantity
            else:
                sells += 1
                sell_quantity += quantity
                sell_notional += price * quantity
            if row.instrument_price is not None:
                direction = 1.0 if delta > 0 else -1.0
                mark_edge += (row.instrument_price - price) * quantity * direction
        previous, end_ns = row, timestamp
        count += 1

    if start_ns is None or end_ns is None:
        raise ValueError("History must include at least one timestamped row")
    duration = end_ns - start_ns
    mean_position = _ratio(position_integral, float(duration))
    position_variance = (
        max(ZERO, squared_position_integral / duration - mean_position ** 2)
        if mean_position is not None else None
    )
    pnl = previous.equity - initial.equity
    total_return = _ratio(pnl, initial.equity)
    fees = previous.trading_fees - initial.trading_fees
    quantity = buy_quantity + sell_quantity
    notional = buy_notional + sell_notional
    daily = _daily_metrics(initial, day_closes, daily_fill_counts, start_ns, end_ns)
    returns = [day["return_fraction"] for day in daily if not day["is_partial_day"] and day["return_fraction"] is not None]
    daily_std = mean_daily_return = None
    if len(returns) >= 2:
        mean_daily_return = sum(returns, ZERO) / len(returns)
        daily_std = sqrt(sum((value - mean_daily_return) ** 2 for value in returns) / (len(returns) - 1))
    annual_scale = sqrt(365)

    return {
        "period": {
            "start_timestamp_ns": start_ns, "end_timestamp_ns": end_ns,
            "elapsed_seconds": float(duration) / 1_000_000_000,
            "snapshot_count": count,
        },
        "performance": {
            "initial_equity": initial.equity, "final_equity": previous.equity,
            "total_pnl": pnl,
            "realized_pnl": previous.realized_pnl - initial.realized_pnl,
            "unrealized_pnl": previous.unrealized_pnl - initial.unrealized_pnl,
            "net_realized_pnl": previous.net_realized_pnl - initial.net_realized_pnl,
            "execution_pnl": previous.execution_pnl - initial.execution_pnl,
            "inventory_pnl": previous.inventory_pnl - initial.inventory_pnl,
            "fees_funding_pnl": previous.fees_funding_pnl - initial.fees_funding_pnl,
            "trading_fees": fees, "funding_pnl": previous.funding_pnl - initial.funding_pnl,
            "total_return_fraction": total_return,
            "annualized_return_fraction_linear": total_return * YEAR_NS / duration if total_return is not None and duration > 0 else None,
            "funding_settlement_count": settlements,
        },
        "daily": daily,
        "inventory": {
            "final_position_size": previous.position_size,
            "final_position_balance": previous.position_balance,
            "min_position_size": min_position, "max_position_size": max_position,
            "max_abs_position_size": max(abs(min_position), abs(max_position)),
            "mean_position_size_time_weighted": mean_position,
            "mean_abs_position_size_time_weighted": _ratio(abs_position_integral, float(duration)),
            "std_position_size_time_weighted": sqrt(position_variance) if position_variance is not None else None,
            "max_abs_position_balance": max_abs_balance,
            "mean_abs_position_balance_time_weighted": _ratio(abs_balance_integral, float(duration)),
            "long_time_fraction": _ratio(float(long_ns), float(duration)),
            "short_time_fraction": _ratio(float(short_ns), float(duration)),
            "flat_time_fraction": _ratio(float(duration - long_ns - short_ns), float(duration)),
        },
        "fills": {
            "fill_count": buys + sells, "buy_fill_count": buys, "sell_fill_count": sells,
            "orders_with_fills": len(filled_ids),
            "buy_quantity": buy_quantity, "sell_quantity": sell_quantity,
            "total_quantity": quantity,
            "buy_notional": buy_notional, "sell_notional": sell_notional,
            "total_notional": notional,
            "mean_fill_quantity": _ratio(quantity, float(buys + sells)),
            "buy_vwap": _ratio(buy_notional, buy_quantity),
            "sell_vwap": _ratio(sell_notional, sell_quantity),
            "turnover_over_initial_equity": _ratio(notional, initial.equity),
            "effective_fee_bps": fees / notional * 10_000 if notional else None,
            "gross_fill_edge_at_mark": mark_edge,
            "mean_fill_edge_bps": mark_edge / notional * 10_000 if notional else None,
        },
        "risk": {
            "min_equity": min_equity, "max_equity": max_equity,
            "max_drawdown": max_drawdown, "max_drawdown_fraction": max_drawdown_fraction,
            "max_drawdown_duration_seconds": float(max_drawdown_duration_ns) / 1_000_000_000,
            "max_abs_exposure_fraction": max_exposure,
            "mean_abs_exposure_fraction_time_weighted": _ratio(exposure_integral, float(positive_equity_ns)),
            "nonpositive_equity_observed": nonpositive_equity,
            "complete_daily_return_count": len(returns),
            "annualized_daily_volatility": daily_std * annual_scale if daily_std is not None else None,
            "annualized_daily_sharpe": mean_daily_return / daily_std * annual_scale if daily_std else None,
            "best_daily_pnl": max(day["total_pnl"] for day in daily),
            "worst_daily_pnl": min(day["total_pnl"] for day in daily),
        },
    }


def analyze_csv(path: str | Path) -> dict[str, Any]:
    return analyze_history(load_history(path))


def save_metrics(report: dict[str, Any], path: str | Path) -> Path:
    """Save numeric floats and integer timestamps as JSON numbers; undefined ratios are null."""
    output = Path(path)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output


def format_metrics(report: dict[str, Any]) -> str:
    """Compact human-readable summary; JSON contains the full metric set."""
    performance, inventory, fills, risk = (report[name] for name in ("performance", "inventory", "fills", "risk"))

    def percent(value: float | None) -> str:
        return f"{value * 100:.4f}%" if value is not None else "N/A"

    lines = [
        f"Total PnL: ${performance['total_pnl']:.6f}",
        f"Equity: ${performance['initial_equity']:.6f} -> ${performance['final_equity']:.6f}",
        f"Realized trading PnL: ${performance['realized_pnl']:.6f}; unrealized: ${performance['unrealized_pnl']:.6f}",
        f"Execution PnL: ${performance['execution_pnl']:.6f}; inventory: ${performance['inventory_pnl']:.6f}; fees + funding: ${performance['fees_funding_pnl']:.6f}",
        f"Fees: ${performance['trading_fees']:.6f}; funding PnL: ${performance['funding_pnl']:.6f}",
        f"Return: {percent(performance['total_return_fraction'])}; linear return p.a.: {percent(performance['annualized_return_fraction_linear'])}",
        f"Inventory: {inventory['final_position_size']} ETH; max absolute: {inventory['max_abs_position_size']} ETH",
        f"Time long / short / flat: {percent(inventory['long_time_fraction'])} / {percent(inventory['short_time_fraction'])} / {percent(inventory['flat_time_fraction'])}",
        f"Fills: {fills['fill_count']} ({fills['buy_fill_count']} buys, {fills['sell_fill_count']} sells); notional: ${fills['total_notional']:.6f}",
        f"Max drawdown: ${risk['max_drawdown']:.6f} ({percent(risk['max_drawdown_fraction'])})",
        f"Max absolute position / equity: {percent(risk['max_abs_exposure_fraction'])}",
        f"Daily-return Sharpe p.a.: {risk['annualized_daily_sharpe'] if risk['annualized_daily_sharpe'] is not None else 'N/A'}",
        "Daily PnL (UTC):",
    ]
    for day in report["daily"]:
        suffix = " (partial day)" if day["is_partial_day"] else ""
        lines.append(f"  {day['date_utc']}: ${day['total_pnl']:.6f}; {day['fill_count']} fills{suffix}")
    return "\n".join(lines)
