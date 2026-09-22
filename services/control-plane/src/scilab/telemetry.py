from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager

from opentelemetry import baggage, context
from opentelemetry.baggage.propagation import W3CBaggagePropagator


_PROPAGATOR = W3CBaggagePropagator()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value


@contextmanager
def correlation_context(*, run_id: str, lab_id: str) -> Iterator[dict[str, str]]:
    values = {"run_id": _text(run_id, "run_id"), "lab_id": _text(lab_id, "lab_id")}
    otel_context = baggage.set_baggage("run_id", values["run_id"])
    otel_context = baggage.set_baggage("lab_id", values["lab_id"], context=otel_context)
    token = context.attach(otel_context)
    try:
        yield dict(values)
    finally:
        context.detach(token)


def current_correlation() -> dict[str, str]:
    return {
        key: value
        for key in ("run_id", "lab_id")
        if isinstance(value := baggage.get_baggage(key), str)
    }


def inject_correlation(carrier: MutableMapping[str, str]) -> None:
    _PROPAGATOR.inject(carrier)


@contextmanager
def extracted_correlation(
    carrier: MutableMapping[str, str],
    *,
    expected_run_id: str,
    expected_lab_id: str,
) -> Iterator[dict[str, str]]:
    token = context.attach(_PROPAGATOR.extract(carrier))
    try:
        correlation = current_correlation()
        expected = {
            "run_id": _text(expected_run_id, "expected_run_id"),
            "lab_id": _text(expected_lab_id, "expected_lab_id"),
        }
        if correlation != expected:
            raise ValueError("baggage correlation does not match authenticated context")
        yield correlation
    finally:
        context.detach(token)


def correlation_fields() -> dict[str, str]:
    return current_correlation()
