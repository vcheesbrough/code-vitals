"""The OTEL_* rules: absent is off, half-present is a fault, localhost is neither."""

import pytest

from code_vitals.build_info import BuildInfo
from code_vitals.telemetry import (
    TelemetryConfig,
    TelemetryConfigError,
    build_resource,
    parse_resource_attributes,
    read_config,
)

GOOD = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://monitor-alloy:4318",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
}


def test_no_otel_variables_means_off() -> None:
    config = read_config({"PATH": "/bin"})
    assert not config.enabled
    assert config.reason == "no OTEL_* variables set"


def test_sdk_disabled_is_the_explicit_off_even_when_configured() -> None:
    config = read_config({**GOOD, "OTEL_SDK_DISABLED": "TRUE "})
    assert not config.enabled
    assert "OTEL_SDK_DISABLED" in config.reason


def test_a_complete_configuration_exports_all_three_signals() -> None:
    assert read_config(GOOD) == TelemetryConfig(traces=True, metrics=True, logs=True)


def test_one_signal_can_be_silenced() -> None:
    config = read_config({**GOOD, "OTEL_LOGS_EXPORTER": "none"})
    assert (config.traces, config.metrics, config.logs) == (True, True, False)


def test_every_exporter_none_is_off() -> None:
    env = {**GOOD, **{f"OTEL_{s}_EXPORTER": "none" for s in ("TRACES", "METRICS", "LOGS")}}
    assert not read_config(env).enabled


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev"}, "no OTLP endpoint"),
        ({**GOOD, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "only http/protobuf"),
        ({**GOOD, "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc"}, "only http/protobuf"),
        ({**GOOD, "OTEL_EXPORTER_OTLP_ENDPOINT": "monitor-alloy:4318"}, "not a URL"),
        ({**GOOD, "OTEL_TRACES_EXPORTER": "zipkin"}, "only 'otlp' or 'none'"),
        ({**GOOD, "OTEL_TRACES_SAMPLER_ARG": "1.5"}, r"not in \[0, 1\]"),
        ({**GOOD, "OTEL_TRACES_SAMPLER_ARG": "half"}, "not a number"),
        (
            {**GOOD, "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=x"},
            "deployment.environment.name",
        ),
        ({**GOOD, "OTEL_RESOURCE_ATTRIBUTES": "nonsense"}, "malformed entry"),
    ],
)
def test_a_half_configured_set_is_a_fault(env: dict[str, str], message: str) -> None:
    with pytest.raises(TelemetryConfigError, match=message):
        read_config(env)


def test_a_per_signal_endpoint_satisfies_that_signal() -> None:
    env = {
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t:4318/v1/traces",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
    }
    assert read_config(env) == TelemetryConfig(traces=True)


def test_resource_attributes_are_url_decoded() -> None:
    assert parse_resource_attributes("a=b%20c, d=e") == {"a": "b c", "d": "e"}


def test_resource_identity() -> None:
    build = BuildInfo(version="0.4.17", revision="abc")
    env = {
        **GOOD,
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=prod,service.version=9.9.9",
    }
    attributes = build_resource(env, build, "bored").attributes

    assert attributes["service.name"] == "code-vitals-collector"
    assert attributes["service.instance.id"] == "bored"
    assert attributes["deployment.environment.name"] == "prod"
    assert attributes["telemetry_source"] == "otlp"
    # The build's version wins over whatever the deployment claims.
    assert attributes["service.version"] == "0.4.17"
    assert attributes["telemetry.sdk.language"] == "python"


def test_the_deployment_may_name_the_instance_and_the_service() -> None:
    env = {
        **GOOD,
        "OTEL_SERVICE_NAME": "renamed",
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev,service.instance.id=x",
    }
    attributes = build_resource(env, BuildInfo("0.4.1", "r"), "bored").attributes
    assert (attributes["service.name"], attributes["service.instance.id"]) == ("renamed", "x")
