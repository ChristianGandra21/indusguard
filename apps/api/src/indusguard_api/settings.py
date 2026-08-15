"""Configuração do processo carregada do ambiente com defaults seguros.

Arquivos de conector podem referenciar o *nome* de uma variável, mas valores sensíveis pertencem
somente ao ambiente. Essa regra evita que uma API key seja versionada, enviada ao modelo ou
devolvida nos endpoints de catálogo.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    execution_mode: Literal["simulate", "execute"] = "simulate"

    # O caminho pode ser trocado em testes e deployments sem alterar o pacote Python.
    connectors_dir: Path = Field(default_factory=default_connectors_dir)
    api_prefix: str = "/api/v1"
