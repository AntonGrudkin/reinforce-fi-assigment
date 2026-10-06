"""Small PNG dashboard for the position-history CSV; Matplotlib is optional."""

import csv
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .history import ATTRIBUTION_COLUMNS, OrderEvent, _add_attribution_columns, load_order_history
from .models import Number, OrderStatus, Side, as_float, milliseconds_to_ns

if TYPE_CHECKING:
    from matplotlib.axes import Axes


COLUMNS = (
    "position_size", "position_balance", "equity", "total_pnl",
    "net_realized_pnl", "unrealized_pnl", "trading_fees", "funding_pnl",
    *ATTRIBUTION_COLUMNS,
)

T = TypeVar('T')


def _sample_interval_ns(seconds: Number | None) -> int | None:
    if seconds is None:
        return None
    interval = milliseconds_to_ns(as_float(seconds) * 1000)
    if interval <= 0:
        raise ValueError("Plot sample interval must be positive, or None for full resolution")
    return interval


def _sample_by_time(
    rows: Iterable[T], timestamp_ns: Callable[[T], int | None], interval_ns: int | None,
) -> Iterator[T]:
    """Keep the last observation in each elapsed-time bucket, plus both endpoints.

    Retain untimed initial rows. Keep each selected row's actual timestamp; never
    shift a bucket's last observation backward to its beginning or fill data gaps.
    Scan every timestamp for regressions, but retain only a pending bucket row.
    """
    start_ns = previous_ns = bucket = None
    first = pending = None
    for row in rows:
        timestamp = timestamp_ns(row)
        if timestamp is None:
            if previous_ns is not None:
                raise ValueError("Untimed plot rows must precede timestamped rows")
            yield row
            continue
        if previous_ns is not None and timestamp < previous_ns:
            raise ValueError("History CSV timestamps must be chronological")
        previous_ns = timestamp
        if interval_ns is None:
            yield row
            continue
        if start_ns is None:
            start_ns, first = timestamp, row
            yield row
        current_bucket = (timestamp - start_ns) // interval_ns
        if bucket is not None and current_bucket != bucket and pending is not first:
            yield pending
        bucket, pending = current_bucket, row
    if interval_ns is not None and pending is not None and pending is not first:
        yield pending


def _read_plot_rows(
    path: str | Path, columns: Iterable[str], interval_ns: int | None,
    *, with_attribution: bool = False,
) -> list[dict[str, str]]:
    """Sample raw CSV rows before numeric conversion to avoid loading the full history."""
    def checked_lines(source):
        # Detect sparse-file corruption before csv tries to parse a huge NUL field.
        # Bounded reads also avoid allocating the entire corrupted line at once.
        pieces = []
        while chunk := source.readline(64 * 1024):
            if '\0' in chunk:
                raise ValueError(
                    f"History CSV {path} contains null bytes and is corrupted; "
                    "regenerate it in a new output directory."
                )
            pieces.append(chunk)
            if chunk.endswith(('\n', '\r')):
                yield ''.join(pieces)
                pieces.clear()
        if pieces:
            yield ''.join(pieces)

    with Path(path).open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(checked_lines(source))
        missing = set(columns) - set(reader.fieldnames or [])
        if with_attribution:
            missing -= set(ATTRIBUTION_COLUMNS)
        if missing:
            raise ValueError(f"History CSV is missing columns: {', '.join(sorted(missing))}")
        rows = reader
        if with_attribution and not set(ATTRIBUTION_COLUMNS) <= set(reader.fieldnames or []):
            # Legacy attribution needs every fill and mark change, before sampling.
            required = {'event', 'fill_quantity', 'fill_price'}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"History CSV is missing columns: {', '.join(sorted(missing))}")
            rows = _add_attribution_columns(reader)
        return list(_sample_by_time(rows, lambda row: int(row['timestamp_ns']) if row['timestamp_ns'] else None, interval_ns))


def _add_price_overlay(
    axis: "Axes", hours: list[float], prices: list[float], *, right_offset: float = 0,
) -> "Axes":
    """Draw known prices behind the main series on an independent right scale.

    Offset the price spine when the panel already has a right-hand quantity axis.
    NaNs leave unknown prices as gaps; the shaded area starts at the known minimum.
    """
    price_axis = axis.twinx()
    price_axis.spines['right'].set_position(('outward', right_offset))
    price_axis.set_ylabel('Mid / mark (USD)', color='#6b7280', fontsize=9)
    price_axis.tick_params(axis='y', colors='#6b7280', labelsize=8)
    price_axis.ticklabel_format(axis='y', style='plain', useOffset=False)
    price_axis.step(hours, prices, where='post', color='#6b7280', alpha=0.3,
                    linewidth=0.9, label='Mid / mark')
    known = [price for price in prices if isfinite(price)]
    if known:
        price_axis.fill_between(hours, prices, min(known), step='post',
                                color='#6b7280', alpha=0.08)
    price_axis.set_zorder(0)
    axis.set_zorder(1)
    axis.patch.set_visible(False)
    return price_axis


def plot_history(
    csv_path: str | Path, png_path: str | Path = "position_history.png",
    *, sample_interval_seconds: Number | None = 1.0,
) -> Path:
    """Write six vertically aligned panels with faint right-axis price overlays.

    Base and quote positions share a panel, with quote USD on the first right axis
    and instrument price on an outward-offset second right axis.
    Monetary values use floats throughout; the input CSV is untouched.
    Time differences are calculated in integer nanoseconds before float conversion.
    Instrument price is the latest known book midpoint (or the runtime's trade
    fallback before a usable book). Unknown prices are left as gaps.
    Average entry shares the price panel; flat positions are shown as gaps.
    By default draw the last row per elapsed second, plus initial/first/final rows.
    None restores full resolution. CSV sampling happens before float conversion.
    Older CSVs get execution/inventory attribution at full resolution first.
    """
    rows = _read_plot_rows(csv_path, {"timestamp_ns", "instrument_price", "average_entry_price", *COLUMNS},
                           _sample_interval_ns(sample_interval_seconds), with_attribution=True)
    if not rows:
        raise ValueError("History CSV has no data rows")

    timestamps = [int(row["timestamp_ns"]) if row["timestamp_ns"] else None for row in rows]
    timed = [timestamp for timestamp in timestamps if timestamp is not None]
    if not timed:
        raise ValueError("History CSV has no timestamped rows")
    if any(current < previous for previous, current in zip(timed, timed[1:])):
        raise ValueError("History CSV timestamps must be chronological")
    start_ns = timed[0]
    # Anchor the untimed initial allocation to the first event for plotting.
    elapsed_ns = [(timestamp if timestamp is not None else start_ns) - start_ns for timestamp in timestamps]
    hours = [elapsed / 3_600_000_000_000 for elapsed in elapsed_ns]
    values = {column: [float(row[column]) for row in rows] for column in COLUMNS}
    instrument_prices = [float(row["instrument_price"]) if row["instrument_price"] else float("nan") for row in rows]
    # Accounting stores zero when flat; use NaN (Matplotlib's None-like gap)
    # rather than drawing a zero price or carrying the last entry forward.
    entry_prices = [
        float(row["average_entry_price"]) if position != 0 and row["average_entry_price"] else float("nan")
        for row, position in zip(rows, values["position_size"])
    ]

    # Import lazily so the runtime remains usable without the plotting dependency.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(12, 17))
    FigureCanvasAgg(figure)
    axes = figure.subplots(6, 1, sharex=True)
    base, equity, pnl, attribution, costs, price = axes
    quote = base.twinx()
    quote.set_zorder(2)
    quote.patch.set_visible(False)

    base.step(hours, values["position_size"], where="post", color="#2563eb", label="Base position")
    base.set(title="Position in base and quote currency", ylabel="Base position (ETH)")
    base.tick_params(axis='y', colors='#2563eb')
    base.yaxis.label.set_color('#2563eb')
    quote.plot(hours, values["position_balance"], color="#d97706", label="Marked quote position")
    quote.set_ylabel("Quote position (USD)", color='#d97706')
    quote.tick_params(axis='y', colors='#d97706', labelsize=8)
    quote.ticklabel_format(axis='y', style='plain', useOffset=False)
    base.legend(handles=[base.lines[0], quote.lines[0]], fontsize=8, ncols=2)
    equity.plot(hours, values["equity"], color="#15803d")
    equity.set(title="Equity", ylabel="USD")
    equity.ticklabel_format(axis="y", style="plain", useOffset=False)

    for column, label, color in (
        ("total_pnl", "Total", "#15803d"),
        ("net_realized_pnl", "Realized, net of costs", "#2563eb"),
        ("unrealized_pnl", "Unrealized", "#d97706"),
    ):
        pnl.plot(hours, values[column], label=label, color=color)
    pnl.set(title="PnL breakdown", ylabel="USD")
    pnl.legend(fontsize=8)

    for column, label, color in (
        ("execution_pnl", "Spread / execution", "#2563eb"),
        ("inventory_pnl", "Inventory", "#d97706"),
        ("fees_funding_pnl", "Fees + funding (signed)", "#7c3aed"),
        ("total_pnl", "Total", "#15803d"),
    ):
        attribution.plot(hours, values[column], label=label, color=color)
    attribution.set(title="Cumulative PnL attribution", ylabel="USD")
    attribution.legend(fontsize=8, ncols=4)

    costs.step(hours, values["trading_fees"], where="post", label="Fee cost", color="#dc2626")
    costs.step(hours, values["funding_pnl"], where="post", label="Funding PnL (signed)", color="#7c3aed")
    costs.set(title="Cumulative fees and settled funding", ylabel="USD")
    costs.legend(fontsize=8)

    price.set_title("Instrument price and average entry price")
    price.yaxis.set_visible(False)  # This panel's price series uses its right axis.

    for axis in axes:
        price_axis = _add_price_overlay(axis, hours, instrument_prices, right_offset=65 if axis is base else 0)
        if axis is price:
            price_axis.step(hours, entry_prices, where="post", label="Average entry price", color="#d97706", linewidth=1.2)
            price_axis.legend(fontsize=8)
        axis.grid(alpha=0.2)
        axis.tick_params(labelsize=8)
    for axis in axes[:-1]:
        axis.tick_params(labelbottom=False)
    price.set_xlabel("Elapsed time (hours)")
    start = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc)
    figure.suptitle(f"Position history · {start:%Y-%m-%d %H:%M:%S} UTC", fontsize=14)
    figure.tight_layout(pad=0.8, h_pad=1.3, rect=(0, 0, 1, 0.97))
    output = Path(png_path)
    figure.savefig(output, format="png", dpi=150)
    figure.clear()
    return output


Segments = dict[Side, list[tuple[tuple[float, float], tuple[float, float]]]]


def _resting_segments(
    records: list[OrderEvent], start_ns: int, end_ns: int,
    *, sample_interval_ns: int | None = None,
) -> tuple[Segments, Segments]:
    """Reconstruct each order's actual lifetime; never bridge cancellations."""
    prices: Segments = {side: [] for side in Side}
    sizes: Segments = {side: [] for side in Side}
    active: dict[int, OrderEvent] = {}
    lifetimes: dict[Side, list[tuple[int, int, OrderEvent]]] = {side: [] for side in Side}

    def segment(previous: OrderEvent, until_ns: int) -> None:
        if until_ns <= previous.timestamp_ns:
            return
        lifetimes[previous.side].append((previous.timestamp_ns, until_ns, previous))

    for record in records:
        if record.event == "place":
            if record.order_id in active:
                raise ValueError("Duplicate placement for an active order")
            active[record.order_id] = record
        elif record.event == 'reject':
            continue  # Rejected requests never start a resting-order lifetime.
        elif record.event in ("fill", "cancel"):
            previous = active.get(record.order_id)
            if previous is None:
                raise ValueError("Order history is missing a placement before a fill/cancel")
            segment(previous, record.timestamp_ns)
            if record.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                active[record.order_id] = record
            else:
                del active[record.order_id]
        else:
            raise ValueError(f"Unknown order history event: {record.event}")
    for record in active.values():
        segment(record, end_ns)
    for side in Side:
        segments = lifetimes[side]
        if sample_interval_ns is not None:
            # Reconstruct before sampling: dropping lifecycle events could invent
            # orders surviving a cancellation. Keep true endpoints of chosen spans.
            segments.sort(key=lambda item: item[0])
        selected = (segments if sample_interval_ns is None else
                    _sample_by_time(segments, lambda item: item[0], sample_interval_ns))
        for from_ns, until_ns, previous in selected:
            start, end = (from_ns - start_ns) / 3_600_000_000_000, (until_ns - start_ns) / 3_600_000_000_000
            prices[side].append(((start, previous.price), (end, previous.price)))
            sizes[side].append(((start, previous.remaining_quantity), (end, previous.remaining_quantity)))
    return prices, sizes


def plot_order_history(
    csv_path: str | Path, png_path: str | Path = "order_history.png",
    position_history_path: str | Path | None = None,
    *, tick_size: Number = "0.01", sample_interval_seconds: Number | None = 1.0,
) -> Path:
    """Plot absolute prices, centered placements and remaining quantities to a PNG.

    Optional position CSV from the same run supplies the full midpoint timeline
    and run end for still-open orders. Otherwise mark samples are limited to order
    events and open quotes extend to the last order event. The centered panel
    shows (price - contemporaneous midpoint) / tick_size for placements and
    best bid/ask. Supply the strategy's tick size; the default is 0.01 USD.
    Half-tick offsets are possible when the midpoint falls between price ticks.
    Older CSVs without bid_price_1/ask_price_1 still plot, without book bands.
    Timestamps stay integer until elapsed-time conversion; numeric offsets use floats.
    Sample market overlays, per-side placement markers and reconstructed resting
    spans once per second by default (plus endpoints). Retain all fill markers.
    None restores full resolution; sampling never changes order CSVs or lifetimes.
    Each vertically aligned panel includes a faint right-axis midpoint overlay.
    """
    tick_size = as_float(tick_size)
    if tick_size <= 0:
        raise ValueError("Tick size must be positive")
    interval_ns = _sample_interval_ns(sample_interval_seconds)
    records = list(load_order_history(csv_path))
    market: list[tuple[int, float]] = []
    market_times: list[int] = []
    book_offsets: dict[Side, list[tuple[int, float]]] = {side: [] for side in Side}

    def record_book(timestamp_ns: int, bid: float | None, ask: float | None) -> None:
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2
            book_offsets[Side.BUY].append((timestamp_ns, (bid - mid) / tick_size))
            book_offsets[Side.SELL].append((timestamp_ns, (ask - mid) / tick_size))

    if position_history_path is not None:
        for row in _read_plot_rows(position_history_path, ('timestamp_ns', 'instrument_price'), interval_ns):
            if row['timestamp_ns']:
                timestamp = int(row['timestamp_ns'])
                market_times.append(timestamp)
                if row['instrument_price']:
                    market.append((timestamp, as_float(row['instrument_price'])))
                record_book(timestamp,
                            as_float(row['bid_price_1']) if row.get('bid_price_1') else None,
                            as_float(row['ask_price_1']) if row.get('ask_price_1') else None)
    else:
        for row in _sample_by_time(records, lambda row: row.timestamp_ns, interval_ns):
            if row.instrument_price is not None:
                market.append((row.timestamp_ns, row.instrument_price))
            record_book(row.timestamp_ns, row.bid_price_1, row.ask_price_1)
    timestamps = [row.timestamp_ns for row in records] + market_times
    if not timestamps:
        raise ValueError("No timestamped order or market observations to plot")
    start_ns, end_ns = min(timestamps), max(timestamps)
    prices, sizes = _resting_segments(records, start_ns, end_ns, sample_interval_ns=interval_ns)

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection
    from matplotlib.figure import Figure

    figure = Figure(figsize=(12, 11), layout="constrained")
    FigureCanvasAgg(figure)
    price_axis, relative_axis, size_axis = figure.subplots(3, 1, sharex=True)
    relative_values: list[float] = []
    for side, color, marker in ((Side.BUY, "#2563eb", "^"), (Side.SELL, "#dc2626", "v")):
        price_axis.add_collection(LineCollection(prices[side], colors=color, linewidths=1, alpha=0.7, label=f"{side.value.title()} limits"))
        size_axis.add_collection(LineCollection(sizes[side], colors=color, linewidths=1, alpha=0.7, label=f"{side.value.title()} remaining"))
        placements = list(_sample_by_time(
            (row for row in records if row.event == "place" and row.side == side),
            lambda row: row.timestamp_ns, interval_ns))
        times = [(row.timestamp_ns - start_ns) / 3_600_000_000_000 for row in placements]
        price_axis.scatter(times, [float(row.price) for row in placements], color=color, marker=marker, s=4, alpha=0.35)
        size_axis.scatter(times, [float(row.remaining_quantity) for row in placements], color=color, marker=marker, s=4, alpha=0.35)
        centered = []
        for row in placements:
            mid = ((row.bid_price_1 + row.ask_price_1) / 2
                   if row.bid_price_1 is not None and row.ask_price_1 is not None
                   else row.instrument_price)
            if mid is not None:
                centered.append(((row.timestamp_ns - start_ns) / 3_600_000_000_000, float((row.price - mid) / tick_size)))
        if centered:
            relative_axis.scatter([point[0] for point in centered], [point[1] for point in centered],
                                  color=color, marker=marker, s=7, alpha=0.5, label=f"{side.value.title()} placements")
            relative_values.extend(point[1] for point in centered)
        offsets = book_offsets[side]
        if offsets:
            relative_axis.step([(timestamp - start_ns) / 3_600_000_000_000 for timestamp, _ in offsets],
                               [offset for _, offset in offsets], where="post", color=color,
                               linewidth=0.8, alpha=0.2, label="Best bid" if side == Side.BUY else "Best ask")
            relative_values.extend(offset for _, offset in offsets)
    if market:
        price_axis.step(
            [(timestamp - start_ns) / 3_600_000_000_000 for timestamp, _ in market],
            [price for _, price in market], where="post", color="#374151", linewidth=0.8,
            label="Mid / mark" if position_history_path else "Mid / mark at order events",
        )
    fills = [row for row in records if row.event == "fill"]
    if fills:
        times = [(row.timestamp_ns - start_ns) / 3_600_000_000_000 for row in fills]
        price_axis.scatter(times, [float(row.fill_price) for row in fills], color="#15803d", marker="x", s=18, label="Fills")
        size_axis.scatter(times, [float(row.fill_quantity) for row in fills], color="#15803d", marker="x", s=18, label="Fill quantity")
    if not records:
        price_axis.text(0.5, 0.5, "No order activity", ha="center", transform=price_axis.transAxes)
    price_axis.set(title="Resting limit orders and executions", ylabel="Price (USD)")
    price_axis.ticklabel_format(axis="y", style="plain", useOffset=False)
    relative_axis.axhline(0, color="#374151", linewidth=0.8, linestyle="--", label="Midpoint")
    relative_axis.set(title=f"Order placements relative to midpoint (tick = {tick_size} USD)",
                      ylabel="Price − mid (ticks)")
    # Equal positive/negative limits keep the midpoint visually centered.
    extent = max((abs(value) for value in relative_values), default=0) or 1
    relative_axis.set_ylim(-extent * 1.05, extent * 1.05)
    if not any(book_offsets.values()):
        relative_axis.text(0.02, 0.95, "Best bid/ask unavailable in this CSV; rerun to record them",
                           va="top", transform=relative_axis.transAxes, fontsize=8)
    size_axis.set(title="Remaining quantity per resting order", ylabel="Quantity (ETH)", xlabel="Elapsed time (hours)")
    for axis in (price_axis, relative_axis, size_axis):
        axis.autoscale_view()
        axis.grid(alpha=0.2)
        axis.legend(fontsize=8)
        _add_price_overlay(axis,
                           [(timestamp - start_ns) / 3_600_000_000_000 for timestamp, _ in market],
                           [price for _, price in market])
    start = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc)
    figure.suptitle(f"Order history · {start:%Y-%m-%d %H:%M:%S} UTC", fontsize=14)
    output = Path(png_path)
    figure.savefig(output, format="png", dpi=150)
    figure.clear()
    return output
