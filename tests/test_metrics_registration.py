"""Registration guards: the two namespaces a shared metric catalog has to police.

Split from test_metrics.py at the 500-line cap. The division is by concern rather
than by size: that file covers what the catalog *is* -- namespacing, contents,
build, gauge observation -- and this one covers what registration *refuses*.

There are two namespaces and they are independent. A short name is the key a call
site passes; a suffix is the published metric name. Guarding only the first let a
distinct short name point at a suffix the catalog already exported, which builds
two instruments under one exported name -- not an error downstream, which is what
makes it worth refusing here.
"""

from __future__ import annotations

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.obs import metrics

from tests.metrics_helpers import FakeMeter, observe


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
    assert [o.value for o in observe(meter, "acme.jobs")] == [2]


@pytest.mark.parametrize("register", ["register_counter", "register_histogram"])
def test_registering_over_a_catalog_name_is_refused(register: str) -> None:
    """Allowing it creates the shared series and then never writes to it.

    `build()` still builds the base instrument, then overwrites `built[name]` with
    the registered one -- so `acme.jobs.total` exists, stays empty forever, and
    every `counter_add("jobs_total")` call site updates the worker's own series
    instead. Reproduced before this guard existed.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    with pytest.raises(ValueError, match="shared catalog instrument"):
        getattr(metrics, register)(
            "jobs_total", "custom.jobs", description="Mine", unit="1"
        )

    # Exactly once: the catalog's own entry, with nothing added beside it.
    assert metrics.instrument_names().count("jobs_total") == 1


def test_a_catalog_name_cannot_be_shadowed_across_instrument_kinds() -> None:
    """The worst spelling of the collision: a counter call site gets a histogram."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    with pytest.raises(ValueError):
        metrics.register_histogram(
            "jobs_total", "custom.jobs", description="Mine", unit="s"
        )

    meter = FakeMeter()
    built = metrics.build(meter)
    assert built["jobs_total"] == "counter:acme.jobs.total"


def test_one_short_name_cannot_be_both_a_counter_and_a_histogram() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_counter("thing", "thing.total", description="Thing")

    with pytest.raises(ValueError, match="already registered as a counter"):
        metrics.register_histogram("thing", "thing.duration", description="Thing")


def test_a_histogram_name_cannot_later_be_registered_as_a_counter() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_histogram("thing", "thing.duration", description="Thing")

    with pytest.raises(ValueError, match="already registered as a histogram"):
        metrics.register_counter("thing", "thing.total", description="Thing")


def test_a_unique_short_name_may_not_reuse_a_catalog_suffix() -> None:
    """Short names and exported suffixes are separate namespaces.

    Guarding only the short name let `register_histogram("custom_jobs",
    "jobs.total")` through, and `build()` then created acme.jobs.total as both a
    counter and a histogram. Downstream that is not an error -- the samples merge
    or become indistinguishable depending on the backend -- which is exactly why it
    has to be refused here.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    with pytest.raises(ValueError, match="already exported by"):
        metrics.register_histogram(
            "custom_jobs", "jobs.total", description="Mine", unit="s"
        )

    meter = FakeMeter()
    metrics.build(meter)
    every = meter.counters + meter.histograms + list(meter.gauges)
    assert every.count("acme.jobs.total") == 1


def test_a_registered_gauge_may_not_reuse_a_gpu_gauge_suffix() -> None:
    """The GPU gauges are built unconditionally, so their suffixes are claimed."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    with pytest.raises(ValueError, match="built-in GPU gauge"):
        metrics.register_gauge(
            "gpu.utilization_percent", lambda: 5, description="Mine"
        )


def test_two_registrations_may_not_share_a_suffix() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_counter("first", "shared.total", description="First")

    with pytest.raises(ValueError, match="registered counter 'first'"):
        metrics.register_counter("second", "shared.total", description="Second")


def test_a_registration_may_keep_its_own_suffix_when_replaced() -> None:
    """Re-registering a name with the suffix it already has is not a collision.

    Without the self-exclusion the guard would refuse a worker re-running its own
    boot path -- which is what an entry point imported twice under test does.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_counter("thing", "thing.total", description="First")
    metrics.register_counter("thing", "thing.total", description="Second")

    meter = FakeMeter()
    metrics.build(meter)
    assert meter.counters.count("acme.thing.total") == 1


def test_a_gauge_may_be_re_registered_with_the_same_suffix() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_gauge("ready", lambda: 1, description="First")
    metrics.register_gauge("ready", lambda: 0, description="Second")

    meter = FakeMeter()
    metrics.build(meter)
    assert [o.value for o in observe(meter, "acme.ready")] == [0]


def test_re_registering_an_extra_instrument_replaces_rather_than_duplicates() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_histogram("h", "first.duration", description="First", unit="s")
    metrics.register_histogram("h", "second.duration", description="Second", unit="s")
    meter = FakeMeter()

    metrics.build(meter)

    assert "acme.second.duration" in meter.histograms
    assert "acme.first.duration" not in meter.histograms
    assert metrics.instrument_names().count("h") == 1
