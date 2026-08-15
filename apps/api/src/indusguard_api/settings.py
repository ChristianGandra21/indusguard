"""Configuração da aplicação carregada exclusivamente do ambiente."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_connectors_dir() -> Path:
    """Resolve o diretório de conectores quando a API roda a partir do monorepo."""

    return Path(__file__).resolve().parents[4] / "connectors"


class Settings(BaseSettings):
    """Configuração sem segredos serializáveis ou expostos pela API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INDUSGUARD_",
        extra="ignore",
    )

    environment: str = "development"
    execution_mode: Literal["simulate", "execute"] = "simulate"
    connectors_dir: Path = Field(default_factory=default_connectors_dir)
    api_prefix: str = "/api/v1"
