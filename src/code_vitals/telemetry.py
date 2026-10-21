"""The telemetry module: the ONLY place the OpenTelemetry SDK and exporters appear.

Product code emits through the OpenTelemetry API (code_vitals.obs) and
stdlib logging under the `code_vitals` logger; this module decides whether any
of it leaves the process, and how. Contract: agent-shared `observability`
skill; Python binding: its references/python.md.

- Configuration is the standard OTEL_* variables only. None set → telemetry
  off (the CLI says so once). Any set → the whole set is validated and a
  half-configured one fails startup (TelemetryConfigError). OTEL_SDK_DISABLED
  =true is the explicit off.
- OTLP over http/protobuf is the only egress; the gRPC exporter is not shipped.
- Identity: service.name, service.instance.id (the measured repo unless the
  variables carry one), deployment.environment.name (required),
  telemetry_source=otlp, and service.version from the build — not overridable.
- Shutdown flushes the three signals in parallel daemon threads joined within
  one fixed budget, so an unreachable or black-holed collector can never hold the
  process (a CI step) open.
"""

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from code_vitals.build_info import BuildInfo

SERVICE_NAME = "code-vitals-collector"
# The logger product code logs under. The OTLP handler goes on this logger and
# never on the root logger: on root it would also capture the OTLP exporter's
# own retry warnings, and with the collector unreachable the log flush then
# deadlocks (reproduced; references/python.md).
PRODUCT_LOGGER = "code_vitals"
SHUTDOWN_BUDGET_SECONDS = 5.0
# A run lasts seconds to tens of minutes; the exit flush is the export, so the
# periodic one should never fire first.
METRIC_EXPORT_INTERVAL_MILLIS = 6 * 60 * 60 * 1000
# Durations are recorded in seconds; the SDK's default boundaries suit ms.
DURATION_BUCKETS_SECONDS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600,
)  # fmt: skip

_SIGNALS = ("traces", "metrics", "logs")
_PROTOCOL = "http/protobuf"
REQUIRED_RESOURCE_ATTRIBUTES = ("deployment.environment.name",)


class TelemetryConfigError(ValueError):
    """The OTEL_* variables are present but not a usable configuration."""


@dataclass(frozen=True)
class TelemetryConfig:
    traces: bool = False
    metrics: bool = False
    logs: bool = False
    reason: str = ""

    @property
    def enabled(self) -> bool:
        return self.traces or self.metrics or self.logs


def parse_resource_attributes(raw: str) -> dict[str, str]:
    """OTEL_RESOURCE_ATTRIBUTES: comma-separated key=value, values URL-decoded."""
    from urllib.parse import unquote

    attributes = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise TelemetryConfigError(f"OTEL_RESOURCE_ATTRIBUTES: malformed entry {item!r}")
        attributes[key.strip()] = unquote(value.strip())
    return attributes


def read_config(environ: Mapping[str, str]) -> TelemetryConfig:
    """Decide from the environment whether telemetry is on, and validate it if so."""
    if not any(key.startswith("OTEL_") for key in environ):
        return TelemetryConfig(reason="no OTEL_* variables set")
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return TelemetryConfig(reason="OTEL_SDK_DISABLED=true")

    exporting = {}
    for signal in _SIGNALS:
        name = f"OTEL_{signal.upper()}_EXPORTER"
        value = environ.get(name, "otlp").strip().lower()
        if value not in ("otlp", "none"):
            raise TelemetryConfigError(f"{name}={value!r}: only 'otlp' or 'none' are supported")
        exporting[signal] = value == "otlp"
    if not any(exporting.values()):
        return TelemetryConfig(reason="every OTEL_*_EXPORTER is none")

    for name in ["OTEL_EXPORTER_OTLP_PROTOCOL"] + [
        f"OTEL_EXPORTER_OTLP_{s.upper()}_PROTOCOL" for s in _SIGNALS
    ]:
        protocol = environ.get(name)
        if protocol is not None and protocol.strip() != _PROTOCOL:
            raise TelemetryConfigError(f"{name}={protocol!r}: only {_PROTOCOL} is shipped")

    for signal in _SIGNALS:
        if not exporting[signal]:
            continue
        endpoint = environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT") or environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", ""
        )
        # Never let the SDK's localhost default apply: a missing endpoint
        # while a signal exports is a fault.
        if not endpoint:
            raise TelemetryConfigError(
                f"no OTLP endpoint for {signal}: set OTEL_EXPORTER_OTLP_ENDPOINT "
                f"(or OTEL_{signal.upper()}_EXPORTER=none)"
            )
        if not endpoint.startswith(("http://", "https://")):
            raise TelemetryConfigError(f"OTLP endpoint for {signal} is not a URL: {endpoint!r}")

    sampler_arg = environ.get("OTEL_TRACES_SAMPLER_ARG")
    if sampler_arg is not None:
        try:
            ratio = float(sampler_arg)
        except ValueError:
            raise TelemetryConfigError(
                f"OTEL_TRACES_SAMPLER_ARG={sampler_arg!r} is not a number"
            ) from None
        if not 0.0 <= ratio <= 1.0:
            raise TelemetryConfigError(f"OTEL_TRACES_SAMPLER_ARG={sampler_arg!r} is not in [0, 1]")

    attributes = parse_resource_attributes(environ.get("OTEL_RESOURCE_ATTRIBUTES", ""))
    for key in REQUIRED_RESOURCE_ATTRIBUTES:
        if not attributes.get(key):
            raise TelemetryConfigError(f"OTEL_RESOURCE_ATTRIBUTES must set {key}")

    return TelemetryConfig(
        traces=exporting["traces"], metrics=exporting["metrics"], logs=exporting["logs"]
    )


def build_resource(environ: Mapping[str, str], build: BuildInfo, instance_id: str) -> Resource:
    defaults = {
        "service.name": environ.get("OTEL_SERVICE_NAME") or SERVICE_NAME,
        "service.instance.id": instance_id,
    }
    from_environment = parse_resource_attributes(environ.get("OTEL_RESOURCE_ATTRIBUTES", ""))
    # Not the deployment's to state: the build knows its version, and the
    # platform marks pushed telemetry by this source.
    forced = {"service.version": build.version, "telemetry_source": "otlp"}
    # Resource.create({}) contributes only the SDK's telemetry.sdk.* attributes
    # here; ours, merged last, win.
    return Resource.create({}).merge(Resource({**defaults, **from_environment, **forced}))


class Telemetry:
    """The running telemetry of one process; `shutdown()` once at exit."""

    def __init__(
        self,
        config: TelemetryConfig,
        shutdowns: Iterable[Callable[[], object]] = (),
        handler: logging.Handler | None = None,
    ) -> None:
        self.config = config
        self._shutdowns = list(shutdowns)
        self._handler = handler

    def shutdown(self, budget_seconds: float = SHUTDOWN_BUDGET_SECONDS) -> bool:
        """Flush and stop every signal; True if it finished within the budget."""
        if self._handler is not None:
            logging.getLogger(PRODUCT_LOGGER).removeHandler(self._handler)
        if not self._shutdowns:
            return True

        def stop(shutdown: Callable[[], object]) -> None:
            # An export failure must never fail the run.
            with contextlib.suppress(Exception):
                shutdown()

        # One thread per signal, all against one deadline: a stalled trace
        # export cannot use up the budget the metrics and logs need.
        workers = [
            threading.Thread(target=stop, args=(s,), name="telemetry-shutdown", daemon=True)
            for s in self._shutdowns
        ]
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + budget_seconds
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in workers)


def setup(environ: Mapping[str, str], build: BuildInfo, instance_id: str) -> Telemetry:
    """Start telemetry from the environment; raises TelemetryConfigError."""
    config = read_config(environ)
    if not config.enabled:
        return Telemetry(config)
    resource = build_resource(environ, build, instance_id)
    shutdowns: list[Callable[[], object]] = []
    handler = None

    if config.traces:
        tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
        shutdowns.append(tracer_provider.shutdown)

    if config.metrics:
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(), export_interval_millis=METRIC_EXPORT_INTERVAL_MILLIS
        )
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
            views=[
                View(
                    instrument_type=Histogram,
                    aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS_SECONDS),
                )
            ],
            shutdown_on_exit=False,
        )
        metrics.set_meter_provider(meter_provider)
        _register_build_info(build)
        shutdowns.append(meter_provider.shutdown)

    if config.logs:
        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        product = logging.getLogger(PRODUCT_LOGGER)
        product.addHandler(handler)
        product.setLevel(logging.INFO)
        shutdowns.append(logger_provider.shutdown)

    return Telemetry(config, shutdowns, handler)


def _register_build_info(build: BuildInfo) -> None:
    def observe(_options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(1, {"version": build.version, "revision": build.revision})]

    metrics.get_meter(PRODUCT_LOGGER).create_observable_gauge(
        "code_vitals_collector.build.info",
        callbacks=[observe],
        description="Always 1; the build's version and revision as attributes, to join on.",
    )
