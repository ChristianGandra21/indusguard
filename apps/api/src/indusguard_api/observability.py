"""OpenTelemetry do produto com saídas gratuitas e atributos deliberadamente mínimos.

O restante do sistema recebe esta abstração por injeção. Assim, testes e usos internos podem
desativar telemetria sem alterar o fluxo do agente, enquanto uma instalação observável compartilha
o mesmo contexto entre run, modelo, tool, policy e HTTP.

Nenhum helper deste módulo aceita prompts, bodies, headers ou credenciais. Essa escolha de API
torna mais difícil vazar conteúdo sensível por acidente.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode

ComponentState = Literal["disabled", "configured", "recorded", "failed"]
AttributeValue = (
    str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Estado sanitizado dos exportadores que pode aparecer no resultado da run."""

    enabled: bool
    local_trace: ComponentState
    otlp: ComponentState

    @property
    def degraded(self) -> bool:
        return self.local_trace == "failed" or self.otlp == "failed"


class Telemetry(Protocol):
    """Pequena fronteira usada pelo domínio, sem expor configuração dos exportadores."""

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> AbstractContextManager[Span]:
        """Inicia um span que herda automaticamente o trace ativo."""

    def snapshot(self) -> TelemetrySnapshot:
        """Retorna apenas saúde operacional, nunca endpoint ou credenciais."""

    def force_flush(self, timeout_millis: int = 3000) -> bool:
        """Tenta entregar spans pendentes sem transformar falha em exceção de domínio."""

    def shutdown(self) -> None:
        """Libera workers dos exportadores."""


def mark_span_error(span: Span, code: str) -> None:
    """Marca falha usando somente um código estável e redigido."""

    span.set_attribute("error.type", code)
    span.set_status(Status(StatusCode.ERROR, code))


class NoOpTelemetry:
    """Default explícito: mantém os contratos sem criar arquivo ou acesso de rede."""

    def __init__(self) -> None:
        self._tracer = trace.NoOpTracerProvider().get_tracer("indusguard")

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> AbstractContextManager[Span]:
        return self._tracer.start_as_current_span(name, attributes=dict(attributes or {}))

    def snapshot(self) -> TelemetrySnapshot:
        return TelemetrySnapshot(enabled=False, local_trace="disabled", otlp="disabled")

    def force_flush(self, timeout_millis: int = 3000) -> bool:
        del timeout_millis
        return True

    def shutdown(self) -> None:
        return None


class JsonlSpanExporter(SpanExporter):
    """Grava uma linha JSON por span sem depender de um backend externo."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            lines = [
                json.dumps(self._serialize(span), ensure_ascii=False, sort_keys=True)
                for span in spans
            ]
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as destination:
                    for line in lines:
                        destination.write(f"{line}\n")
            return SpanExportResult.SUCCESS
        except (OSError, TypeError, ValueError):
            return SpanExportResult.FAILURE

    @staticmethod
    def _serialize(span: ReadableSpan) -> dict[str, Any]:
        context = span.context
        parent = span.parent
        return {
            "trace_id": f"{context.trace_id:032x}" if context else None,
            "span_id": f"{context.span_id:016x}" if context else None,
            "parent_span_id": f"{parent.span_id:016x}" if parent else None,
            "name": span.name,
            "start_time_unix_nano": span.start_time,
            "end_time_unix_nano": span.end_time,
            "status": span.status.status_code.name,
            "attributes": dict(span.attributes or {}),
        }


class _TrackingExporter(SpanExporter):
    """Converte o resultado de qualquer exporter em saúde consultável pelo runtime."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate
        self._state: ComponentState = "configured"
        self._lock = Lock()

    @property
    def state(self) -> ComponentState:
        with self._lock:
            return self._state

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._delegate.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        with self._lock:
            self._state = "recorded" if result is SpanExportResult.SUCCESS else "failed"
        return result

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:
            with self._lock:
                self._state = "failed"
            return False

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:
            with self._lock:
                self._state = "failed"


def _parse_otlp_headers(raw: str | None) -> dict[str, str] | None:
    """Interpreta ``chave=valor`` separado por vírgula sem jamais registrar o conteúdo."""

    if raw is None or not raw.strip():
        return None
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ValueError("INDUSGUARD_OTLP_HEADERS precisa usar chave=valor")
        headers[key.strip()] = value.strip()
    return headers


class OpenTelemetryRuntime:
    """Provider isolado por instância, adequado a testes e ao futuro host FastAPI."""

    def __init__(
        self,
        *,
        service_name: str,
        jsonl_path: Path | None,
        otlp_endpoint: str | None = None,
        otlp_headers: str | None = None,
    ) -> None:
        if jsonl_path is None and otlp_endpoint is None:
            raise ValueError("ao menos um exportador OpenTelemetry precisa estar configurado")

        self._provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        self._local: _TrackingExporter | None = None
        self._otlp: _TrackingExporter | None = None

        if jsonl_path is not None:
            self._local = _TrackingExporter(JsonlSpanExporter(jsonl_path))
            # JSONL síncrono torna uma falha local visível na mesma run.
            self._provider.add_span_processor(SimpleSpanProcessor(self._local))

        if otlp_endpoint is not None:
            self._otlp = _TrackingExporter(
                OTLPSpanExporter(
                    endpoint=otlp_endpoint,
                    headers=_parse_otlp_headers(otlp_headers),
                )
            )
            # OTLP usa batch para não adicionar uma chamada de rede a cada span.
            self._provider.add_span_processor(BatchSpanProcessor(self._otlp))

        self._tracer = self._provider.get_tracer("indusguard", "0.1.0")

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> AbstractContextManager[Span]:
        return self._tracer.start_as_current_span(name, attributes=dict(attributes or {}))

    def snapshot(self) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            enabled=True,
            local_trace=self._local.state if self._local else "disabled",
            otlp=self._otlp.state if self._otlp else "disabled",
        )

    def force_flush(self, timeout_millis: int = 3000) -> bool:
        try:
            return bool(self._provider.force_flush(timeout_millis))
        except Exception:
            return False

    def shutdown(self) -> None:
        self._provider.shutdown()


def telemetry_from_settings(settings: Any) -> Telemetry:
    """Monta telemetria a partir de Settings sem fazer o módulo depender desse modelo."""

    if settings.otlp_enabled and not settings.otlp_endpoint:
        raise ValueError("INDUSGUARD_OTLP_ENDPOINT é obrigatório quando OTLP está ativo")
    jsonl_path = settings.trace_jsonl_path if settings.trace_jsonl_enabled else None
    otlp_endpoint = settings.otlp_endpoint if settings.otlp_enabled else None
    if jsonl_path is None and otlp_endpoint is None:
        return NoOpTelemetry()
    return OpenTelemetryRuntime(
        service_name=settings.telemetry_service_name,
        jsonl_path=jsonl_path,
        otlp_endpoint=otlp_endpoint,
        otlp_headers=settings.otlp_headers,
    )
