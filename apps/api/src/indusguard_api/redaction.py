"""Redaction compartilhada antes de persistência, traces ou respostas estruturadas."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED_VALUE: Final = "[REDACTED]"
DEFAULT_SENSITIVE_FIELDS: Final = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
    }
)
_LABELED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|secret|token)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def redact_text(value: str) -> str:
    """Remove formatos explícitos comuns sem alegar detectar qualquer PII em texto livre."""

    value = _BEARER_SECRET.sub(f"Bearer {REDACTED_VALUE}", value)
    return _LABELED_SECRET.sub(lambda match: f"{match.group(1)}={REDACTED_VALUE}", value)


def redact_value(
    value: Any,
    fields: Sequence[str] = (),
    *,
    redact_strings: bool = False,
) -> Any:
    """Redige chaves recursivamente; opcionalmente aplica máscara conservadora em strings."""

    sensitive = DEFAULT_SENSITIVE_FIELDS | {field.lower() for field in fields}
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED_VALUE
                if str(key).lower() in sensitive
                else redact_value(child, sensitive, redact_strings=redact_strings)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_value(child, sensitive, redact_strings=redact_strings) for child in value]
    if isinstance(value, tuple):
        return tuple(
            redact_value(child, sensitive, redact_strings=redact_strings) for child in value
        )
    if redact_strings and isinstance(value, str):
        return redact_text(value)
    return value
