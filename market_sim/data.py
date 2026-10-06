"""Lazy parquet readers matching source/README.md; only these readers need PyArrow."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from heapq import merge
from math import isfinite
from pathlib import Path
from typing import Any

from .models import BookSnapshot, Event, FundingUpdate, TimerEvent, Trade, event_key


NANOSECONDS = 1_000_000_000
DAY_NS = 24 * 3600 * NANOSECONDS


@dataclass
class EventWindow(Iterator[Event]):
    """Lazy events plus exact requested/effective bounds; contains no future prices."""

    start_timestamp_ns: int
    end_timestamp_ns: int
    requested_end_timestamp_ns: int
    events: Iterator[Event]

    def __iter__(self) -> "EventWindow":
        return self

    def __next__(self) -> Event:
        return next(self.events)


def load_window(data: Path, start_date: str, hours: float) -> tuple[EventWindow, int]:
    """Load a daily parquet window, capped at the last available book timestamp.

    Normal windows are [start, requested_end). If capped, include the final book
    and other events at its timestamp. A terminal timer closes accounting at the
    effective end. Missing files inside that range still raise; missing trailing
    dates beyond book coverage do not. Only timestamps are inspected ahead of time.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if not isfinite(hours) or hours <= 0:
        raise ValueError('hours must be finite and positive')
    duration_ns = round(hours * 3600 * NANOSECONDS)
    if duration_ns <= 0:
        raise ValueError('hours must cover at least one nanosecond')
    first_day = date.fromisoformat(start_date)
    start_ns = (first_day - date(1970, 1, 1)).days * DAY_NS
    requested_end_ns = start_ns + duration_ns
    first_book = data / 'orderbook' / f'{first_day.isoformat()}.parquet'
    if not first_book.is_file():
        raise FileNotFoundError(f'Missing file for the requested window: {first_book}')

    # Daily ISO filenames are chronological. Scan just the final nonempty book
    # file's timestamp column, preserving ns (Python datetime loses sub-us bits).
    last_book_ns = None
    book_paths = sorted(path for path in (data / 'orderbook').glob('*.parquet')
                        if path.name >= first_book.name)
    for path in reversed(book_paths):
        for batch in pq.ParquetFile(path).iter_batches(columns=['datetime']):
            timestamps = batch.column(0).cast(pa.timestamp('ns')).cast(pa.int64())
            maximum = pc.max(timestamps).as_py()
            if maximum is not None:
                last_book_ns = maximum if last_book_ns is None else max(last_book_ns, maximum)
        if last_book_ns is not None:
            break
    if last_book_ns is None or last_book_ns <= start_ns:
        raise ValueError('Orderbook data must cover a positive period after the requested start')
    end_ns = min(requested_end_ns, last_book_ns)
    trimmed = requested_end_ns > last_book_ns
    duration_ns = end_ns - start_ns
    day_count = (duration_ns // DAY_NS + 1 if trimmed
                 else (duration_ns + DAY_NS - 1) // DAY_NS)
    days = [first_day + timedelta(days=i) for i in range(day_count)]
    paths = {stream: [data / stream / f'{day.isoformat()}.parquet' for day in days]
             for stream in ('orderbook', 'trades', 'fundings')}
    for stream_paths in paths.values():
        for path in stream_paths:
            if not path.is_file():
                raise FileNotFoundError(f'Missing file for the effective window: {path}')

    total = 1  # Terminal timer; runtime-generated settlements are excluded.
    for stream_paths in paths.values():
        for day, path in zip(days, stream_paths):
            parquet = pq.ParquetFile(path)
            day_end_ns = (day - date(1970, 1, 1)).days * DAY_NS + DAY_NS
            if day_end_ns <= end_ns:
                total += parquet.metadata.num_rows
            else:
                for batch in parquet.iter_batches(columns=['datetime']):
                    timestamps = batch.column(0).cast(pa.timestamp('ns')).cast(pa.int64())
                    before_end = (pc.less_equal(timestamps, end_ns) if trimmed
                                  else pc.less(timestamps, end_ns))
                    inside = pc.and_(pc.greater_equal(timestamps, start_ns), before_end)
                    total += pc.sum(inside).as_py() or 0

    def events() -> Iterator[Event]:
        streams = merge_events(load_orderbooks(paths['orderbook']), load_trades(paths['trades']),
                               load_fundings(paths['fundings']))
        for event in streams:
            if event.timestamp_ns > end_ns or (event.timestamp_ns == end_ns and not trimmed):
                break
            if event.timestamp_ns >= start_ns:
                yield event
        yield TimerEvent(end_ns, 'backtest_window_end')

    return EventWindow(start_ns, end_ns, requested_end_ns, events()), total


def _rows(paths: Iterable[Path], columns: list[str]) -> Iterator[dict[str, Any]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    # ISO-date filenames sort chronologically. Unsorted rows are rejected by merge_events.
    for path in sorted(paths):
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=4096, columns=columns):
            timestamp_index = batch.schema.get_field_index("datetime")
            timestamps = batch.column(timestamp_index)
            if pa.types.is_timestamp(timestamps.type):
                timestamps = timestamps.cast(pa.timestamp("ns", tz=timestamps.type.tz))
            # Convert before Python datetime conversion, which can lose nanoseconds.
            batch = batch.set_column(timestamp_index, "datetime", timestamps.cast(pa.int64()))
            yield from batch.to_pylist()


def load_orderbooks(paths: Iterable[Path]) -> Iterator[BookSnapshot]:
    columns = ["datetime"] + [
        f"{side}_{field}_{level}"
        for side in ("bid", "ask")
        for field in ("price", "qty")
        for level in range(1, 21)
    ]
    for row in _rows(paths, columns):
        yield BookSnapshot(
            timestamp_ns=row["datetime"],
            bids=tuple((row[f"bid_price_{i}"], row[f"bid_qty_{i}"]) for i in range(1, 21)),
            asks=tuple((row[f"ask_price_{i}"], row[f"ask_qty_{i}"]) for i in range(1, 21)),
        )


def load_trades(paths: Iterable[Path]) -> Iterator[Trade]:
    for row in _rows(paths, ["datetime", "price", "size", "is_maker_ask"]):
        if row["is_maker_ask"] not in (0, 1):
            raise ValueError("is_maker_ask must be 0 or 1")
        yield Trade(row["datetime"], row["price"], row["size"], bool(row["is_maker_ask"]))


def load_fundings(paths: Iterable[Path]) -> Iterator[FundingUpdate]:
    for row in _rows(paths, ["datetime", "funding_rate"]):
        yield FundingUpdate(row["datetime"], row["funding_rate"])


def ordered_events(events: Iterable[Event]) -> Iterator[Event]:
    previous: tuple[int, int] | None = None
    for event in events:
        key = event_key(event)
        if previous is not None and key < previous:
            raise ValueError("Events must be sorted by timestamp and event tie priority")
        previous = key
        yield event


def merge_events(*streams: Iterable[Event]) -> Iterator[Event]:
    """Merge sorted streams, preserving row/source order for identical event keys.

    Equal timestamps use book, funding update, trade, timer order. The runtime
    inserts funding settlements between funding updates and trades. Streams are peeked
    internally but the runtime exposes only the event currently being processed.
    """
    yield from merge(*(ordered_events(stream) for stream in streams), key=event_key)
