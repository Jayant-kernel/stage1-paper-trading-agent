import threading

import pytest

from stage1.metrics import RecorderCounters


def test_recorder_counters_are_thread_safe() -> None:
    counters = RecorderCounters()

    def increment_all() -> None:
        for _ in range(1_000):
            counters.received_tick()
            counters.control_message()
            counters.quarantined_message()
            counters.written_parquet_records(1)

    workers = [threading.Thread(target=increment_all) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    snapshot = counters.snapshot()
    assert snapshot.received_ticks == 4_000
    assert snapshot.control_messages == 4_000
    assert snapshot.quarantined_messages == 4_000
    assert snapshot.written_parquet_records == 4_000


def test_recorder_counters_reject_negative_increments() -> None:
    counters = RecorderCounters()
    with pytest.raises(ValueError, match="cannot be negative"):
        counters.written_parquet_records(-1)
