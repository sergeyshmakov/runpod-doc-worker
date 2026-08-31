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


# -----------------------------------------------------------------------------
# The base catalog is the intersection, not the union
# -----------------------------------------------------------------------------

# Every metric suffix the two workers written before this package existed export,
# transcribed from their own catalogs. They arrived at these independently and
# spelled all 13 identically, which is the evidence that they are shared rather
# than one worker's choices promoted to everyone's.
SHARED_SUFFIXES = frozenset({
    "jobs.total",
    "pages.total",
    "bytes_in.total",
    "bytes_out.total",
    "errors.total",
    "worker.cold_starts.total",
    "worker.refresh.total",
    "degraded.total",
    "job.duration",
    "phase.duration",
    "pages_per_second",
    "input.size_bytes",
    "output.size_bytes",
    "worker.warmup.duration",
    "gpu.memory_used_bytes",
    "gpu.memory_total_bytes",
    "gpu.utilization_percent",
})


def test_the_base_catalog_holds_only_what_every_adopter_shares() -> None:
    """A suffix here that some workers do not measure is a bug, not a bonus.

    A sidecar startup histogram was in the base list at first. Only some document
    workers run a sidecar -- one of the two existing ones does not, and neither
    will a pipeline-based engine -- so those workers would have been handed an
    instrument that can only ever export an empty series. Anything not universal
    belongs behind `register_counter` / `register_histogram`.
    """
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    metrics.build(meter)

    built_suffixes = {
        name[len("acme.") :]
        for name in meter.counters + meter.histograms + list(meter.gauges)
    }
    unshared = built_suffixes - SHARED_SUFFIXES
    assert not unshared, (
        f"the base catalog exports {sorted(unshared)}, which not every adopting "
        f"worker measures. Register it from the worker that has it instead."
    )


def test_a_worker_can_add_an_instrument_the_others_do_not_have() -> None:
    """The sidecar case: registered by the workers that run one, absent elsewhere."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_histogram(
        "sidecar_startup_duration",
        "sidecar.startup.duration",
        description="Time for an engine sidecar to report ready",
        unit="s",
    )
    metrics.register_counter(
        "sidecar_restarts_total", "sidecar.restarts.total", description="Restarts"
    )
    meter = FakeMeter()

    built = metrics.build(meter)

    assert "acme.sidecar.startup.duration" in meter.histograms
    assert "acme.sidecar.restarts.total" in meter.counters
    # Addressable by short name, exactly like a base instrument -- otherwise a
    # call site would have to know whether its metric came from the catalog or
    # from its own registration.
    assert "sidecar_startup_duration" in built
    assert "sidecar_restarts_total" in built
    assert set(built) == set(metrics.instrument_names())


def test_an_unregistered_worker_gets_none_of_another_workers_instruments() -> None:
    """Registration is per-process state, so it must not leak between workers."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    metrics.build(meter)

    assert not any("sidecar" in name for name in meter.histograms + meter.counters)


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


def test_build_fails_with_a_named_extra_when_opentelemetry_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail at boot with a message, not at the first export tick with silence.

    The gauge callbacks run off the request path, so a missing dependency there is
    not an error anyone sees -- it is every gauge series quietly vanishing.
    """
    import builtins

    real_import = builtins.__import__

    def refuse_opentelemetry(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError("No module named 'opentelemetry'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_opentelemetry)
    config.configure(config.WorkerConfig(env_prefix="ACME"))

    with pytest.raises(RuntimeError, match=r"runpod-doc-worker\[metrics\]"):
        metrics.build(FakeMeter())


def test_re_registering_an_extra_instrument_replaces_rather_than_duplicates() -> None:
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    metrics.register_histogram("h", "first.duration", description="First", unit="s")
    metrics.register_histogram("h", "second.duration", description="Second", unit="s")
    meter = FakeMeter()

    metrics.build(meter)

    assert "acme.second.duration" in meter.histograms
    assert "acme.first.duration" not in meter.histograms
    assert metrics.instrument_names().count("h") == 1


def test_no_gauges_registered_still_builds() -> None:
    """A worker with no state gauges at all -- docling has no sidecar -- is valid."""
    config.configure(config.WorkerConfig(env_prefix="ACME"))
    meter = FakeMeter()

    built = metrics.build(meter)

    assert set(built) == set(metrics.instrument_names())
    assert metrics.registered_gauges() == ()
