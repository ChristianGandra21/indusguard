"""Configuração do processo carregada do ambiente com defaults seguros.

Arquivos de conector podem referenciar o *nome* de uma variável, mas valores sensíveis pertencem
somente ao ambiente. Essa regra evita que uma API key seja versionada, enviada ao modelo ou
devolvida nos endpoints de catálogo.
"""

from pathlib import Path
from typing import Final

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from indusguard_api.schemas import ExecutionMode

PUBLIC_RUN_CONNECTOR_ALLOWLIST: Final = frozenset({"synthetic", "tractian"})


def default_connectors_dir() -> Path:
    """Resolve ``connectors/`` sem depender do diretório em que o comando foi executado."""

    return Path(__file__).resolve().parents[4] / "connectors"


class Settings(BaseSettings):
    """Configuração do runtime, sobrescrevível por variáveis ``INDUSGUARD_*``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INDUSGUARD_",
        extra="ignore",
    )

    # Simulação é o default porque uma instalação nova nunca deve executar mutações apenas por
    # ter recebido uma solicitação do agente.
    environment: str = "development"
    execution_mode: ExecutionMode = "simulate"

    # O caminho pode ser trocado em testes e deployments sem alterar o pacote Python.
    connectors_dir: Path = Field(default_factory=default_connectors_dir)
    api_prefix: str = "/api/v1"

    # O frontend estático roda em outra origem durante desenvolvimento. Uma allowlist explícita
    # permite leitura pelo navegador sem transformar CORS em uma falsa camada de autenticação.
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )

    # SQLite mantém o desenvolvimento autônomo. Em produção, a mesma interface recebe uma URL
    # PostgreSQL/Neon sem espalhar detalhes do banco pelo runtime do agente.
    database_url: str = "sqlite+aiosqlite:///./.data/indusguard.db"
    persist_runs: bool = True

    # JSONL é a saída local gratuita e auditável. OTLP só é ativado explicitamente; endpoint e
    # headers são segredos operacionais e nunca entram em spans ou respostas.
    trace_jsonl_enabled: bool = True
    trace_jsonl_path: Path = Path(".data/traces.jsonl")
    otlp_enabled: bool = False
    otlp_endpoint: str | None = None
    otlp_headers: str | None = None
    telemetry_service_name: str = "indusguard-api"

    # O caminho público nasce desligado. Token e chave do modelo permanecem somente no processo;
    # nenhum deles é parâmetro do frontend nem parte da OpenAPI.
    public_runs_enabled: bool = False
    public_connector_ids: list[str] = Field(default_factory=lambda: ["synthetic"])
    public_run_owner_id: str = Field(default="portfolio-owner", pattern=r"^[A-Za-z0-9_-]{1,128}$")
    owner_token: SecretStr | None = None
    public_run_rate_limit_per_hour: int = Field(default=3, ge=1, le=100)
    public_run_concurrency: int = Field(default=2, ge=1, le=10)

    @field_validator("cors_allowed_origins")
    @classmethod
    def reject_wildcard_cors(cls, value: list[str]) -> list[str]:
        """Recusa wildcard porque CORS não substitui uma política explícita de acesso."""

        normalized = [origin.rstrip("/") for origin in value]
        if "*" in normalized:
            raise ValueError("cors_allowed_origins não aceita wildcard")
        return normalized

    @model_validator(mode="after")
    def validate_public_runtime(self) -> "Settings":
        if len(self.public_connector_ids) != len(set(self.public_connector_ids)):
            raise ValueError("public_connector_ids não aceita duplicatas")
        if not self.public_connector_ids:
            raise ValueError("public_connector_ids precisa declarar ao menos um conector")
        unsupported = set(self.public_connector_ids) - PUBLIC_RUN_CONNECTOR_ALLOWLIST
        if unsupported:
            supported = ", ".join(sorted(PUBLIC_RUN_CONNECTOR_ALLOWLIST))
            raise ValueError(f"public_connector_ids contém conectores não suportados: {supported}")
        if self.public_runs_enabled and (
            self.owner_token is None or len(self.owner_token.get_secret_value()) < 32
        ):
            raise ValueError(
                "INDUSGUARD_OWNER_TOKEN precisa ter ao menos 32 caracteres "
                "quando runs públicas estão ativas"
            )
        if self.public_runs_enabled and self.execution_mode != "simulate":
            raise ValueError("runs públicas exigem INDUSGUARD_EXECUTION_MODE=simulate")
        return self
