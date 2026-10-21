"""A fake OTLP/HTTP receiver: an in-process http.server that decodes what the
exporters send with opentelemetry-proto, so tests assert on what a collector
would actually receive rather than on configuration."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue


def attrs(pairs: Any) -> dict[str, Any]:
    def value(v: AnyValue) -> Any:
        kind = v.WhichOneof("value")
        return getattr(v, kind) if kind else None

    return {kv.key: value(kv.value) for kv in pairs if isinstance(kv, KeyValue)}


@dataclass
class Received:
    traces: list[ExportTraceServiceRequest] = field(default_factory=list)
    metrics: list[ExportMetricsServiceRequest] = field(default_factory=list)
    logs: list[ExportLogsServiceRequest] = field(default_factory=list)

    def spans(self) -> Iterator[tuple[dict[str, Any], Any]]:
        for request in self.traces:
            for rs in request.resource_spans:
                resource = attrs(rs.resource.attributes)
                for ss in rs.scope_spans:
                    for span in ss.spans:
                        yield resource, span

    def log_records(self) -> Iterator[tuple[dict[str, Any], Any]]:
        for request in self.logs:
            for rl in request.resource_logs:
                resource = attrs(rl.resource.attributes)
                for sl in rl.scope_logs:
                    for record in sl.log_records:
                        yield resource, record

    def metric_points(self) -> Iterator[tuple[dict[str, Any], str, dict[str, Any]]]:
        """(resource attributes, metric name, data point attributes)."""
        for request in self.metrics:
            for rm in request.resource_metrics:
                resource = attrs(rm.resource.attributes)
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        data = getattr(metric, metric.WhichOneof("data"))
                        for point in data.data_points:
                            yield resource, metric.name, attrs(point.attributes)


class OtlpReceiver:
    def __init__(self) -> None:
        self.received = Received()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/v1/traces":
                    receiver.received.traces.append(ExportTraceServiceRequest.FromString(body))
                elif self.path == "/v1/metrics":
                    receiver.received.metrics.append(ExportMetricsServiceRequest.FromString(body))
                elif self.path == "/v1/logs":
                    receiver.received.logs.append(ExportLogsServiceRequest.FromString(body))
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> "OtlpReceiver":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
