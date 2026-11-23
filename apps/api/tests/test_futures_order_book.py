import pytest

from app.market_data.futures_order_book import (
    FuturesOrderBookError,
    FuturesOrderBookSynchronizer,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def event(U, u, pu, *, bids=None, asks=None):
    return {
        "e": "depthUpdate",
        "E": 1_800_000_000_000,
        "T": 1_800_000_000_000,
        "s": "BTCUSDT",
        "U": U,
        "u": u,
        "pu": pu,
        "b": bids or [],
        "a": asks or [],
    }


def snapshot(last_id=100):
    return {
        "lastUpdateId": last_id,
        "E": 1_800_000_000_000,
        "T": 1_800_000_000_000,
        "bids": [["100", "2"], ["99", "3"]],
        "asks": [["101", "4"], ["102", "5"]],
    }


def make_sync(clock=None, **kwargs):
    return FuturesOrderBookSynchronizer(
        "BTCUSDT",
        monotonic=clock or FakeClock(),
        **kwargs,
    )


def test_events_are_not_published_before_snapshot():
    sync = make_sync()
    assert sync.ingest_event(event(99, 101, 98, bids=[["100", "3"]]))
    assert sync.book is None
    assert not sync.synchronized


def test_snapshot_bridge_and_sorted_book():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98, bids=[["100", "3"]]))
    book = sync.apply_snapshot(snapshot())

    assert book.book_synchronized is True
    assert book.sequence_contiguous is True
    assert book.last_update_id == 101
    assert book.bids[0] == (book.bids[0][0], book.bids[0][1])
    assert [price for price, _ in book.bids] == sorted(
        (price for price, _ in book.bids), reverse=True
    )
    assert [price for price, _ in book.asks] == sorted(
        price for price, _ in book.asks
    )


def test_events_older_than_snapshot_are_discarded():
    sync = make_sync()
    sync.ingest_event(event(90, 99, 89, bids=[["100", "9"]]))
    sync.ingest_event(event(99, 101, 98, bids=[["100", "3"]]))

    book = sync.apply_snapshot(snapshot(100))
    assert book.last_update_id == 101
    assert dict(book.bids)[100] == 3


def test_subsequent_events_require_previous_update_id():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.ingest_event(event(102, 103, 101, bids=[["100", "4"]]))

    book = sync.apply_snapshot(snapshot())
    assert book.last_update_id == 103
    assert dict(book.bids)[100] == 4


def test_snapshot_without_bridge_fails_closed():
    sync = make_sync()
    sync.ingest_event(event(105, 106, 104))

    with pytest.raises(FuturesOrderBookError):
        sync.apply_snapshot(snapshot(100))

    assert sync.book is None
    assert not sync.synchronized


def test_buffered_sequence_gap_fails_closed():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.ingest_event(event(103, 104, 102))

    with pytest.raises(FuturesOrderBookError):
        sync.apply_snapshot(snapshot())

    assert sync.book is None


def test_live_sequence_gap_invalidates_published_book():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    assert not sync.ingest_event(event(105, 106, 104))
    assert sync.book is None
    assert sync.reason == "SequenceGap"


def test_zero_quantity_deletes_price_level():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    assert sync.ingest_event(
        event(102, 102, 101, bids=[["100", "0"]])
    )
    assert 100 not in dict(sync.book.bids)


def test_new_levels_are_inserted_with_absolute_quantities():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    assert sync.ingest_event(
        event(102, 102, 101, asks=[["103", "7"]])
    )
    assert dict(sync.book.asks)[103] == 7


def test_disconnect_invalidates_book_and_buffer():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    sync.invalidate("WebSocketConnectionLost")

    assert sync.book is None
    assert sync.reason == "WebSocketConnectionLost"


def test_event_buffer_overflow_fails_closed():
    sync = make_sync(max_buffered_events=1)
    sync.ingest_event(event(99, 101, 98))

    with pytest.raises(FuturesOrderBookError):
        sync.ingest_event(event(100, 102, 99))

    assert sync.book is None
    assert sync.reason == "EventBufferOverflow"


def test_stale_book_is_not_returned():
    clock = FakeClock()
    sync = make_sync(clock, max_event_age_seconds=2)
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    clock.value = 104.0

    assert sync.book is None
    assert sync.reason == "BookStale"


def test_crossed_snapshot_is_rejected():
    sync = make_sync()
    invalid = snapshot()
    invalid["asks"] = [["100", "1"]]

    with pytest.raises(FuturesOrderBookError):
        sync.apply_snapshot(invalid)

    assert sync.book is None


def test_wrong_symbol_event_is_rejected():
    sync = make_sync()
    invalid = event(99, 101, 98)
    invalid["s"] = "ETHUSDT"

    with pytest.raises(FuturesOrderBookError):
        sync.ingest_event(invalid)


def test_invalid_update_range_is_rejected():
    sync = make_sync()

    with pytest.raises(FuturesOrderBookError):
        sync.ingest_event(event(102, 101, 100))

    assert sync.book is None


def test_snapshot_does_not_skip_first_non_stale_event_when_bridge_is_later():
    sync = make_sync()

    # First event is newer than the snapshot and cannot bridge it.
    sync.ingest_event(event(105, 106, 104))
    # A later event appears to bridge, but must never be selected by skipping.
    sync.ingest_event(event(99, 101, 98))

    with pytest.raises(FuturesOrderBookError):
        sync.apply_snapshot(snapshot(100))

    assert sync.book is None
    assert not sync.synchronized


def test_malformed_live_event_invalidates_previously_synchronized_book():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    invalid = event(102, 103, 101)
    invalid["s"] = "ETHUSDT"

    with pytest.raises(FuturesOrderBookError):
        sync.ingest_event(invalid)

    assert sync.book is None
    assert sync.reason == "InvalidDepthEvent"


def test_duplicate_live_update_invalidates_previously_synchronized_book():
    sync = make_sync()
    sync.ingest_event(event(99, 101, 98))
    sync.apply_snapshot(snapshot())

    # pu matches, but final update ID does not advance the book.
    duplicate = event(101, 101, 101)

    assert sync.ingest_event(duplicate) is False
    assert sync.book is None
    assert sync.reason == "StaleOrDuplicateDepthEvent"
