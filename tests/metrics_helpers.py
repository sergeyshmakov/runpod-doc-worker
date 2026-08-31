"""Test double for an OpenTelemetry meter, shared by the metrics test files.

Deliberately not a Mock: the assertions are about the exact names passed, and a
Mock would happily accept `create_couner` too.
"""

from __future__ import annotations


class FakeMeter:
    """Records what was created instead of exporting anything."""

    def __init__(self) -> None:
        self.counters: list[str] = []
        self.histograms: list[str] = []
        self.gauges: dict[str, list] = {}

    def create_counter(self, name: str, **_: object) -> str:
        self.counters.append(name)
        return f"counter:{name}"

    def create_histogram(self, name: str, **_: object) -> str:
        self.histograms.append(name)
        return f"histogram:{name}"

    def create_observable_gauge(
        self, name: str, *, callbacks: list, **_: object
    ) -> None:
        self.gauges[name] = callbacks


def observe(meter: FakeMeter, name: str) -> list:
    """Run the single callback registered for ``name`` and return its yields."""
    return list(meter.gauges[name][0](None))
