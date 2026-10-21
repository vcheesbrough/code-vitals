"""What product code emits, through the OpenTelemetry API only.

Instruments, attribute keys and span helpers live here so every name is
defined once. The API's proxy tracer and meter bind to whatever providers
code_vitals.telemetry installs (or stay no-ops when telemetry is off).

Metric attribute values are bounded by enumerations in code (Outcome,
Trigger) or by the plugin registry (measure names); identifiers — commit
shas, run ids, paths — go on spans and log records only, never on metrics.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, SpanKind

INSTRUMENTATION_NAME = "code_vitals"

tracer = trace.get_tracer(INSTRUMENTATION_NAME)
meter = metrics.get_meter(INSTRUMENTATION_NAME)


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class Trigger(StrEnum):
    PUSH = "push"
    BACKFILL = "backfill"
    MANUAL = "manual"


# Metric attribute keys: the only keys a metric data point may carry
# (tests/test_telemetry_otlp.py asserts it on what a receiver got).
OUTCOME = "outcome"
TRIGGER = "trigger"
MEASURE = "measure"
METRIC_ATTRIBUTE_KEYS = frozenset({OUTCOME, TRIGGER, MEASURE, "version", "revision"})

# Span attribute keys of our own (semconv names are used where they exist).
REPO = "code_vitals.repo"
COMMIT_SHA = "code_vitals.commit.sha"
HEAD_SHA = "code_vitals.head.sha"
PIPELINE_NUMBER = "code_vitals.ci.pipeline.number"
BACKLOG_COMMITS = "code_vitals.backlog.commits"
COMMITS_MEASURED_ATTRIBUTE = "code_vitals.commits.measured"
GIT_COMMAND = "code_vitals.git.command"

RUNS = meter.create_counter(
    "code_vitals_collector.runs", unit="{run}", description="Collector runs, by outcome."
)
RUN_DURATION = meter.create_histogram(
    "code_vitals_collector.run.duration", unit="s", description="Collector run duration."
)
COMMITS_MEASURED = meter.create_counter(
    "code_vitals_collector.commits.measured",
    unit="{commit}",
    description="Commits measured and recorded.",
)
MEASURE_DURATION = meter.create_histogram(
    "code_vitals_collector.measure.duration",
    unit="s",
    description="Time one measure plugin took at one commit.",
)
BACKLOG = meter.create_gauge(
    "code_vitals_collector.backlog",
    unit="{commit}",
    description="Commits still to measure when a run starts.",
)
LOCK_WAIT = meter.create_histogram(
    "code_vitals_collector.lock.wait",
    unit="s",
    description="Time a run waited for its repo's advisory lock.",
)


@contextmanager
def db_span(operation: str, summary: str) -> Iterator[Span]:
    """A client span for one database call: the operation and a query *name*,
    never SQL text or bound values."""
    with tracer.start_as_current_span(
        f"{operation} {summary}",
        kind=SpanKind.CLIENT,
        attributes={
            "db.system.name": "postgresql",
            "db.operation.name": operation,
            "db.query.summary": summary,
        },
    ) as span:
        yield span


@contextmanager
def git_span(command: str) -> Iterator[Span]:
    with tracer.start_as_current_span(
        f"git {command}", kind=SpanKind.CLIENT, attributes={GIT_COMMAND: command}
    ) as span:
        yield span
