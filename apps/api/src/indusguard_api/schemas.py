"""Contratos públicos da API e dos perfis de conectores."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuthProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "api_key_header", "api_key_query", "bearer", "context_header"]
    name: str | None = None
    env: str | None = None
    context_field: str | None = None

    @model_validator(mode="after")
    def validate_auth_fields(self) -> "AuthProfile":
        if self.type in {"api_key_header", "api_key_query"} and not (self.name and self.env):
            raise ValueError("autenticação por API key exige 'name' e 'env'")
        if self.type == "bearer" and not self.env:
            raise ValueError("autenticação Bearer exige 'env'")
        if self.type == "context_header" and not (self.name and self.context_field):
            raise ValueError("context_header exige 'name' e 'context_field'")
        return self


class OperationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    access: AccessMode | None = None
    risk: RiskLevel | None = None
    permission: str | None = None
    requires_direct_request: bool = False
    requires_confirmation: bool = False
    justification_min_length: Annotated[int, Field(ge=0, le=1000)] = 0
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10
    max_retries: Annotated[int, Field(ge=0, le=2)] = 0
    idempotent: bool = False
    redact_fields: list[str] = Field(default_factory=list)


class ConnectorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    description: str
    openapi: str
    base_url_env: str | None = None
    allowed_base_urls: list[str] = Field(default_factory=list)
    auth: AuthProfile
    operations: dict[str, OperationPolicy]


class OperationSummary(BaseModel):
    operation_id: str
    method: str
    path: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool
    access: AccessMode
    risk: RiskLevel
    permission: str | None = None
    requires_direct_request: bool
    requires_confirmation: bool
    justification_min_length: int
    timeout_seconds: float
    max_retries: int
    idempotent: bool


class ConnectorSummary(BaseModel):
    id: str
    name: str
    description: str
    openapi_version: str
    auth_type: str
    operation_count: int
    enabled_operation_count: int
    context_fields: list[str] = Field(default_factory=list)


class ConnectorDetails(ConnectorSummary):
    operations: list[OperationSummary]


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    connector_count: int


class VersionResponse(BaseModel):
    version: str
    environment: str
    execution_mode: Literal["simulate", "execute"]
