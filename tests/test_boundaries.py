"""Structural rules of the telemetry contract, checked on the source."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "code_vitals"
TELEMETRY_MODULE = SOURCE / "telemetry.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_the_telemetry_module_imports_the_sdk_or_exporters() -> None:
    offenders = {}
    for path in SOURCE.rglob("*.py"):
        if path == TELEMETRY_MODULE:
            continue
        bad = {
            name
            for name in _imports(path)
            if name.startswith(("opentelemetry.sdk", "opentelemetry.exporter"))
        }
        if bad:
            offenders[str(path.relative_to(SOURCE))] = sorted(bad)
    assert offenders == {}


def test_nothing_attaches_a_handler_to_the_root_logger() -> None:
    # On the root logger the OTLP handler also captures the exporter's own
    # retry warnings, and the log flush deadlocks when the collector is down.
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addHandler"
            ):
                target = node.func.value
                is_root = (
                    isinstance(target, ast.Call)
                    and isinstance(target.func, ast.Attribute)
                    and target.func.attr == "getLogger"
                    and not target.args
                )
                assert not is_root, f"{path.name}:{node.lineno} adds a handler to the root logger"
