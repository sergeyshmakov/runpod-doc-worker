"""The metric catalog: namespacing, and the gauge registration that replaces reach-back.

Two things are worth testing here and they are both about sharing.

The namespace, because it is the only reason this catalog can live in a package
used by several workers: the suffixes are identical everywhere and the prefix must
not be, or two endpoints reporting to one collector add their counters together.

The gauge registry, because the version this was extracted from read worker state
by importing the worker's own modules from inside a callback. That import is what
made the catalog unshareable, and an injected getter is what replaces it.
"""

from __future__ import annotations

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.obs import metrics


class FakeMeter:
    """Records what was created instead of exporting anything.

    Deliberately not a Mock: the assertions are about the exact names passed, and
    a Mock would happily accept `create_couner` too.
    """

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

    def create_observable_gauge(self, name: str, *, callbacks: list, **_: object) -> None:
        self.gauges[name] = callbacks


@pytest.fixture(autouse=True)
def _clean_state():
    metrics._reset_for_tests()
    config.reset()
    yield
    metrics._reset_for_tests()
    config.reset()


def _observe(meter: FakeMeter, name: str) -> list:
    """Run the single callback registered for ``name`` and return its yields."""
    return list(meter.gauges[name][0](None))


# -----------------------------------------------------------------------------
# Namespacing
# -----------------------------------------------------------------------------

def test_every_instrument_carries_the_configured_namespace() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME", metric_namespace="acme"))
    # A registered gauge is required, not incidental: without one the
    # registered-gauge loop in `build` never runs, and an earlier version of this
    # test passed while that loop emitted unprefixed names.
    metrics.register_gauge("worker.jobs_since_boot", lambda: 1, description="Jobs")
    meter = FakeMeter()

    metrics.build(meter)

    every = meter.counters + meter.histograms + list(meter.gauges)
    assert every, "build created no instruments at all"
    assert "acme.worker.jobs_since_boot" in meter.gauges
    stray = [name for name in every if not name.startswith("acme.")]
    assert not stray, f"instruments escaped the namespace: {stray}"


def test_the_namespace_falls_back_to_the_env_prefix_lowercased() -> None:
    """A worker that sets only env_prefix still gets its own series names.

    The alternative -- defaulting to a literal like "worker" -- means two adopters
    that both forgot this field export the same series names to the same collector
    and their counters are summed, with nothing anywhere reporting a problem.
    """
    config.configure(config.WorkerConfig(env_prefix="DOCLING"))
    meter = FakeMeter()

    metrics.build(meter)

    assert "docling.jobs.total" in meter.counters
    assert not any(name.startswith("worker.") for name in meter.counters)


def test_an_explicit_namespace_beats_the_env_prefix() -> None:
    config.configure(
        config.WorkerConfig(env_prefix="HUNYUAN_OCR", metric_namespace="hunyuanocr")
    )

    assert config.active().metrics_prefix() == "hunyuanocr"


def test_the_suffixes_do_not_repeat_the_namespace() -> None:
    """Guards the concatenation, which is the easy thing to get wrong twice.

    A suffix accidentally written as "acme.jobs.total" would export
    "acme.acme.jobs.total" -- a name that looks plausible in a dashboard list and
    is wrong everywhere.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    metrics.build(meter)

    for name in meter.counters + meter.histograms + list(meter.gauges):
        assert name.count("acme.") == 1, f"{name} repeats its namespace"


# -----------------------------------------------------------------------------
# The catalog itself
# -----------------------------------------------------------------------------

def test_build_returns_exactly_the_names_call_sites_address() -> None:
    """`instrument_names` is what a worker's telemetry asserts its call sites against.

    If the two drift, a `counter_add("something")` no-ops in production behind a
    one-shot warning -- which is the failure this pairing exists to prevent.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    built = metrics.build(meter)

    assert set(built) == set(metrics.instrument_names())


def test_gauges_are_not_returned_from_build() -> None:
    """Observable gauges are pull-based; nothing addresses them by name.

    Returning them would invite a call site to `histogram_record` a gauge, which
    fails at the meter rather than here.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_gauge("thing", lambda: 1, description="A thing")
    meter = FakeMeter()

    built = metrics.build(meter)

    assert meter.gauges, "no gauges were created"
    # Exact equality, not a set intersection. Intersecting `built` with the
    # meter's keys compared bare suffixes against prefixed names, so it could not
    # fail -- a gauge leaking into `built` as "thing" never collided with
    # "acme.thing". Equality catches an extra key under either spelling.
    assert set(built) == set(metrics.instrument_names())
    for suffix in metrics.registered_gauges():
        assert suffix not in built
        assert f"acme.{suffix}" not in built


def test_the_gpu_gauges_exist_without_a_gpu() -> None:
    """pynvml is absent on the machine running pytest, and that must be fine.

    The instruments are still created -- a worker's dashboard should show an empty
    series rather than a missing one -- and the callbacks yield nothing.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    metrics.build(meter)

    for suffix in (
        "gpu.memory_used_bytes",
        "gpu.memory_total_bytes",
        "gpu.utilization_percent",
    ):
        assert f"acme.{suffix}" in meter.gauges
        assert _observe(meter, f"acme.{suffix}") == []


# -----------------------------------------------------------------------------
# Gauge registration
# -----------------------------------------------------------------------------

def test_a_registered_gauge_is_exported_and_observed() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_gauge(
        "worker.jobs_since_boot", lambda: 7, description="Jobs since boot"
    )
    meter = FakeMeter()

    metrics.build(meter)

    observed = _observe(meter, "acme.worker.jobs_since_boot")
    assert [o.value for o in observed] == [7]


def test_a_getter_returning_none_yields_nothing() -> None:
    """"Not applicable right now" without inventing a sentinel number.

    A sidecar-readiness gauge on a worker with no sidecar configured should report
    nothing, not 0 -- 0 means "configured and broken", which is a different alert.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_gauge("sidecar_ready", lambda: None, description="Ready")
    meter = FakeMeter()

    metrics.build(meter)

    assert _observe(meter, "acme.sidecar_ready") == []


def test_a_getter_that_raises_loses_its_series_not_the_pipeline() -> None:
    """The whole reason the callback swallows exceptions.

    A gauge callback runs on the export tick. If a worker's state getter raises,
    the cost should be that one series, not the export -- and definitely not an
    exception surfacing on a path the request never touches.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    def broken() -> int:
        raise RuntimeError("state is unavailable")

    metrics.register_gauge("broken", broken, description="Broken")
    metrics.register_gauge("fine", lambda: 3, description="Fine")
    meter = FakeMeter()

    metrics.build(meter)

    assert _observe(meter, "acme.broken") == []
    assert [o.value for o in _observe(meter, "acme.fine")] == [3]


def test_registering_the_same_suffix_twice_replaces_rather_than_duplicates() -> None:
    """A worker whose entry point is imported twice under test must not export twice.

    Two callbacks on one series is not a loud failure; it is a metric that reads
    double, which is worse.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_gauge("jobs", lambda: 1, description="First")
    metrics.register_gauge("jobs", lambda: 2, description="Second")
    meter = FakeMeter()

    metrics.build(meter)

    assert metrics.registered_gauges() == ("jobs",)
    assert len(meter.gauges["acme.jobs"]) == 1
    assert [o.value for o in _observe(meter, "acme.jobs")] == [2]


def test_a_getter_is_called_per_tick_not_captured_at_registration() -> None:
    """The gauge reports current state, so the getter must run on each observation."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    counter = {"n": 0}

    def advancing() -> int:
        counter["n"] += 1
        return counter["n"]

    metrics.register_gauge("advancing", advancing, description="Advancing")
    meter = FakeMeter()
    metrics.build(meter)

    first = [o.value for o in _observe(meter, "acme.advancing")]
    second = [o.value for o in _observe(meter, "acme.advancing")]

    assert first == [1]
    assert second == [2]


def test_no_gauges_registered_still_builds() -> None:
    """A worker with no state gauges at all -- docling has no sidecar -- is valid."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    built = metrics.build(meter)

    assert set(built) == set(metrics.instrument_names())
    assert metrics.registered_gauges() == ()
