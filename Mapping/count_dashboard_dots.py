"""
Count red, orange, and green timeline slots for feeder and consumer dashboards.

Matches Dashboard 2 (outage_dashboard2.html) combo heatmap logic:
  - Red    = outage AND current=0 (both signals active)
  - Orange = outage only (no current=0)
  - Green  = current=0 only (no logged outage)

Uses dashboard2 pickle caches when available; otherwise builds them from source data.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import numpy as np

import dashboard2_api as d2


N_HALF = d2.N_HALF


@dataclass
class DotCounts:
    red: int = 0
    orange: int = 0
    green: int = 0
    white: int = 0
    devices: int = 0

    def add_device(self, red: int, orange: int, green: int) -> None:
        self.devices += 1
        self.red += red
        self.orange += orange
        self.green += green
        self.white += N_HALF - red - orange - green


def _bitmap(buf: bytes) -> np.ndarray:
    arr = np.zeros(N_HALF, dtype=np.uint8)
    if buf:
        raw = np.frombuffer(buf, dtype=np.uint8)
        n = min(len(raw), N_HALF)
        arr[:n] = raw[:n]
    return arr


def count_device_slots(outage_buf: bytes, current_buf: bytes) -> tuple[int, int, int]:
    """Return (red, orange, green) slot counts for one device."""
    o = _bitmap(outage_buf)
    c = _bitmap(current_buf)
    red = int((o & c).sum())
    orange = int((o & (1 - c)).sum())
    green = int(((1 - o) & c).sum())
    return red, orange, green


def count_all(
    outage_bm: dict[str, bytes],
    current_bm: dict[str, bytes],
) -> DotCounts:
    totals = DotCounts()
    all_ids = set(outage_bm) | set(current_bm)
    for did in all_ids:
        red, orange, green = count_device_slots(
            outage_bm.get(did, b""),
            current_bm.get(did, b""),
        )
        totals.add_device(red, orange, green)
    return totals


def _wait_for_data(label: str, ready_fn, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while not ready_fn():
        if time.time() > deadline:
            print(f"Timed out waiting for {label} data.", file=sys.stderr)
            sys.exit(1)
        time.sleep(1)


def _print_section(label: str, counts: DotCounts) -> None:
    total_slots = counts.red + counts.orange + counts.green + counts.white
    print(f"\n{label} ({counts.devices:,} devices)")
    for name, value in (
        ("Red", counts.red),
        ("Orange", counts.orange),
        ("Green", counts.green),
        ("White", counts.white),
    ):
        print(f"{name}: {value:,} slots ({_pct(value, total_slots):.2f}%)")


def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def main() -> None:
    print("Loading dashboard2 data …")
    d2.ensure_load_started()

    _wait_for_data("feeder", lambda: d2.FEEDERS is not None)
    _wait_for_data("consumer", lambda: d2.CONSUMERS is not None)

    feeders = d2.FEEDERS
    consumers = d2.CONSUMERS
    assert feeders is not None and consumers is not None

    feeder_counts = count_all(feeders["outage_bm"], feeders["cz_bm"])
    consumer_counts = count_all(consumers["outage_bm"], consumers["current_bm"])

    _print_section("Feeder dashboard", feeder_counts)
    _print_section("Consumer dashboard", consumer_counts)


if __name__ == "__main__":
    main()
