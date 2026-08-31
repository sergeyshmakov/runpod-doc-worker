"""The metric catalog: what a document worker measures, and how it reads a GPU.

This owns *what* the instruments are — names, units, and the callbacks behind
observable gauges. It does not own whether export happens; a worker keeps its own
telemetry module for the exporter wiring and the span/counter API its call sites
use, and calls :func:`build` from there.

Every exported name is ``<metric_namespace>.<suffix>``, with the namespace coming
from :class:`runpod_doc_worker.config.WorkerConfig`. That indirection is the whole
reason this module can be shared: the suffixes are the same measurements in every
document worker, while the namespace is the one part that must differ so two
endpoints reporting to one collector do not add their counters together.

**Every name here is a public contract with an operator's dashboards.** Renaming
a suffix silently breaks a saved query, which is why they are listed in one place
and asserted by tests rather than spelled at each call site.

Callbacks must be cheap, side-effect-free, and tolerant of missing dependencies:
they run on every export tick, and pynvml is not installed on the machine where
pytest runs.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Optional

from runpod_doc_worker import config


__all__ = [
    "build",
    "instrument_names",
    "register_counter",
    "register_gauge",
    "register_histogram",
    "registered_gauges",
]


# Counters and histograms, keyed by the short name a worker's `counter_add` /
# `histogram_record` take. The short name is what call sites use, so renaming an
# exported metric is one edit here.
#
# The second element is the suffix, appended to the configured namespace. It is
# not the full name: see the module docstring.
_COUNTERS: tuple[tuple[str, str, str, str], ...] = (
    ("jobs_total", "jobs.total", "Jobs processed", "1"),
    ("pages_total", "pages.total", "Pages processed", "1"),
    ("bytes_in_total", "bytes_in.total", "Input bytes received", "By"),
    ("bytes_out_total", "bytes_out.total", "Output bytes sent", "By"),
    ("errors_total", "errors.total", "Errors by phase and type", "1"),
    ("cold_starts_total", "worker.cold_starts.total", "Worker process starts", "1"),
    ("refresh_total", "worker.refresh.total", "Worker recycles", "1"),
    (
        "degraded_total",
        "degraded.total",
        "Artifacts a successful response could not carry",
        "1",
    ),
)

_HISTOGRAMS: tuple[tuple[str, str, str, str], ...] = (
    ("job_duration", "job.duration", "End-to-end job duration", "s"),
    ("phase_duration", "phase.duration", "Per-phase duration", "s"),
    ("pages_per_second", "pages_per_second", "Throughput", "1"),
    ("input_size_bytes", "input.size_bytes", "Input size distribution", "By"),
    ("output_size_bytes", "output.size_bytes", "Output size distribution", "By"),
    (
        "warmup_duration",
        "worker.warmup.duration",
        "Boot-time warmup duration",
        "s",
    ),
)

# The two lists above are deliberately the *intersection*, not the union, of what
# the adopting workers measure. Both workers written before this package existed
# arrived at these 13 suffixes independently and spelled every one of them
# identically, which is the evidence that they belong here.
#
# A sidecar startup histogram was in this list at first, and it was wrong: only
# some document workers run a sidecar, and the others would have been handed an
# instrument that can only ever export an empty series. Anything not shared by
# every adopter is registered by the worker that has it -- see `register_counter`
# and `register_histogram`.

_extra_counters: dict[str, tuple[str, str, str]] = {}
_extra_histograms: dict[str, tuple[str, str, str]] = {}


def _refuse_shadowing(name: str, kind: str) -> None:
    """Refuse a short name the catalog or the other registration map already owns.

    Silently allowing it is worse than it first looks. ``build()`` still creates
    the base instrument, then overwrites ``built[name]`` with the registered one --
    so the canonical shared series is created and never written to again, while
    every call site addressing that name updates the worker's own series instead.
    Across kinds it is worse still: registering a histogram under a counter's name
    hands ``counter_add`` a histogram, and ``instrument_names()`` reports the name
    twice.

    A ValueError rather than a `fail()`: this is a worker's own boot-time wiring
    mistake, not caller input, and it should stop the worker rather than degrade it.
    """
    reserved = {n for n, *_ in _COUNTERS} | {n for n, *_ in _HISTOGRAMS}
    if name in reserved:
        raise ValueError(
            f"{name!r} is a shared catalog instrument and cannot be re-registered "
            f"as {kind}. Registering it would leave the shared series empty while "
            f"call sites wrote to yours. Pick a name of your own."
        )
    other = _extra_histograms if kind == "counter" else _extra_counters
    if name in other:
        raise ValueError(
            f"{name!r} is already registered as "
            f"{'a histogram' if kind == 'counter' else 'a counter'}. One short name "
            f"cannot be both, or a call site gets the wrong instrument kind."
        )


def register_counter(
    name: str, suffix: str, *, description: str, unit: str = "1"
) -> None:
    """Add a counter this worker measures and others do not.

    ``name`` is the short name call sites pass; ``suffix`` is appended to the
    namespace. Re-registering the same name as a counter replaces it; a name the
    shared catalog owns, or one already registered as a histogram, is refused.
    """
    _refuse_shadowing(name, "counter")
    _extra_counters[name] = (suffix, description, unit)


def register_histogram(
    name: str, suffix: str, *, description: str, unit: str = "1"
) -> None:
    """Add a histogram this worker measures and others do not.

    The case this exists for: a worker that runs an engine sidecar wants
    ``sidecar.startup.duration``, and a worker that does not should not have it.
    """
    _refuse_shadowing(name, "histogram")
    _extra_histograms[name] = (suffix, description, unit)


def instrument_names() -> tuple[str, ...]:
    """Every short name a call site may pass to `counter_add` / `histogram_record`.

    Exposed so a test can assert that no call site references a metric the catalog
    does not build — which would otherwise no-op in production behind a one-shot
    warning.
    """
    return (
        tuple(name for name, *_ in _COUNTERS)
        + tuple(name for name, *_ in _HISTOGRAMS)
        + tuple(_extra_counters)
        + tuple(_extra_histograms)
    )


# -----------------------------------------------------------------------------
# Worker-state gauges
# -----------------------------------------------------------------------------
#
# Registered by the worker rather than read by reaching back into it, which keeps
# the dependency arrow pointing from the worker into this package. The alternative
# -- this module importing the worker's own modules to read their state -- is what
# the version this was extracted from did for its sidecar-readiness gauge, and it
# is exactly why that catalog could not be shared: a lazy `from worker import
# sidecar` inside a callback is still a hard dependency on a module name that only
# one repository has, deferred rather than removed.
#
# A getter returning None is skipped for that tick, which is how a worker reports
# "not applicable right now" without inventing a sentinel number.

# `Optional[float]` rather than `float | int | None`: this alias is a runtime
# assignment, not an annotation, so `from __future__ import annotations` does not
# stringify it and it is evaluated on import -- under Python 3.10, which CI still
# builds against. int satisfies float under the numeric tower, so nothing is lost.
_GaugeGetter = Callable[[], Optional[float]]

_gauges: dict[str, tuple[str, str, _GaugeGetter]] = {}


def register_gauge(
    suffix: str, getter: _GaugeGetter, *, description: str, unit: str = "1"
) -> None:
    """Register an observable gauge, exported as ``<namespace>.<suffix>``.

    Call at boot, before the exporter is built. Registering the same suffix twice
    replaces the getter rather than exporting the series twice, so a worker that
    re-imports its entry point under test does not accumulate duplicates.
    """
    _gauges[suffix] = (description, unit, getter)


def registered_gauges() -> tuple[str, ...]:
    """Suffixes registered so far. For tests and for the probe response."""
    return tuple(sorted(_gauges))


def _observation_class() -> Any:
    """The OpenTelemetry ``Observation`` type, or a clear error naming the extra.

    Every gauge callback in this module yields one of these, and the callbacks run
    on the export tick rather than on the request path -- so a missing dependency
    there is not a crash a consumer sees, it is every gauge series silently
    disappearing. :func:`build` calls this once so the failure lands at boot with
    a message, instead of at the first tick with none.
    """
    try:
        from opentelemetry.metrics import Observation  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "runpod_doc_worker.obs.metrics needs the OpenTelemetry API to build "
            "instruments. Install `runpod-doc-worker[metrics]`, or the package "
            "that provides the meter you are passing to build()."
        ) from e
    return Observation


def _observe_registered(getter: _GaugeGetter) -> Callable[[Any], Iterator[Any]]:
    """Wrap a getter as an OpenTelemetry gauge callback.

    Exceptions are swallowed deliberately. A gauge callback runs on the export
    tick, off the request path, and a worker whose state getter raises should lose
    that series rather than its metrics pipeline.
    """

    def callback(options: Any) -> Iterator[Any]:  # noqa: ARG001
        from opentelemetry.metrics import Observation  # noqa: PLC0415

        try:
            value = getter()
        except Exception:  # noqa: BLE001
            return
        if value is None:
            return
        yield Observation(value)

    return callback


# -----------------------------------------------------------------------------
# Building the instruments
# -----------------------------------------------------------------------------

def build(meter: Any) -> dict[str, Any]:
    """Create every instrument and return the ones call sites address by name.

    Only counters and histograms are returned. Observable gauges are pull-based:
    the meter holds the callback and nothing calls them by name.

    Raises ``RuntimeError`` if the OpenTelemetry API is missing, rather than
    letting every gauge callback fail on the export tick where nothing reports it.
    """
    _observation_class()
    prefix = config.active().metrics_prefix()
    built: dict[str, Any] = {}

    for name, suffix, description, unit in _COUNTERS:
        built[name] = meter.create_counter(
            f"{prefix}.{suffix}", description=description, unit=unit
        )
    for name, suffix, description, unit in _HISTOGRAMS:
        built[name] = meter.create_histogram(
            f"{prefix}.{suffix}", description=description, unit=unit
        )
    for name, (suffix, description, unit) in _extra_counters.items():
        built[name] = meter.create_counter(
            f"{prefix}.{suffix}", description=description, unit=unit
        )
    for name, (suffix, description, unit) in _extra_histograms.items():
        built[name] = meter.create_histogram(
            f"{prefix}.{suffix}", description=description, unit=unit
        )

    for suffix, (description, unit, getter) in _gauges.items():
        meter.create_observable_gauge(
            f"{prefix}.{suffix}",
            callbacks=[_observe_registered(getter)],
            description=description,
            unit=unit,
        )

    for suffix, callback, description, unit in (
        ("gpu.memory_used_bytes", _observe_gpu_mem_used, "GPU memory in use", "By"),
        ("gpu.memory_total_bytes", _observe_gpu_mem_total, "GPU memory total", "By"),
        ("gpu.utilization_percent", _observe_gpu_util, "GPU SM utilization", "%"),
    ):
        meter.create_observable_gauge(
            f"{prefix}.{suffix}",
            callbacks=[callback],
            description=description,
            unit=unit,
        )
    return built


# -----------------------------------------------------------------------------
# GPU observation
# -----------------------------------------------------------------------------
#
# pynvml needs to be imported and initialized exactly once per process. The init
# has a measurable cost (~30 ms) so it is deferred until first use, and the device
# handles are cached — the count and per-index handles do not change after
# nvmlInit, so re-querying on every export tick wastes work.

_nvml: Any = None
_nvml_init_attempted = False
_nvml_handles: list[tuple[int, Any]] = []


def _get_nvml() -> Any:
    global _nvml, _nvml_init_attempted
    if _nvml_init_attempted:
        return _nvml
    _nvml_init_attempted = True
    try:
        import pynvml  # noqa: PLC0415

        pynvml.nvmlInit()
        _nvml = pynvml
        _cache_handles()
    except Exception:  # noqa: BLE001
        _nvml = None
    return _nvml


def _cache_handles() -> None:
    """Populate ``_nvml_handles`` once per process. Failures leave it empty."""
    if _nvml is None:
        return
    try:
        count = _nvml.nvmlDeviceGetCount()
    except Exception:  # noqa: BLE001
        return
    for i in range(count):
        try:
            _nvml_handles.append((i, _nvml.nvmlDeviceGetHandleByIndex(i)))
        except Exception:  # noqa: BLE001
            return


def _handles() -> Iterable[tuple[int, Any]]:
    if _get_nvml() is None:
        return ()
    return _nvml_handles


def _observe_memory(field: str) -> Iterator[Any]:
    from opentelemetry.metrics import Observation  # noqa: PLC0415

    pynvml = _get_nvml()
    if pynvml is None:
        return
    for idx, handle in _handles():
        try:
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            yield Observation(int(getattr(info, field)), {"device": str(idx)})
        except Exception:  # noqa: BLE001
            continue


def _observe_gpu_mem_used(options: Any) -> Iterator[Any]:  # noqa: ARG001
    yield from _observe_memory("used")


def _observe_gpu_mem_total(options: Any) -> Iterator[Any]:  # noqa: ARG001
    yield from _observe_memory("total")


def _observe_gpu_util(options: Any) -> Iterator[Any]:  # noqa: ARG001
    from opentelemetry.metrics import Observation  # noqa: PLC0415

    pynvml = _get_nvml()
    if pynvml is None:
        return
    for idx, handle in _handles():
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            yield Observation(int(util.gpu), {"device": str(idx)})
        except Exception:  # noqa: BLE001
            continue


def _reset_for_tests() -> None:
    """Drop NVML state and registered gauges. NOT public API."""
    global _nvml, _nvml_init_attempted
    _nvml = None
    _nvml_init_attempted = False
    _nvml_handles.clear()
    _gauges.clear()
    _extra_counters.clear()
    _extra_histograms.clear()
