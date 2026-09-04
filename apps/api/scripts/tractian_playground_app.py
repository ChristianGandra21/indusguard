"""Composition root local para testar o playground Tractian com Gemini.

Este app não entra na wheel de produção. Ele reaproveita o gateway Gemini do pacote de evals para
uma sessão manual consentida, mantém o runtime em ``simulate`` e publica somente o conector
``tractian``.
"""

import os
from pathlib import Path

from indusguard_evals.gemini_gateway import GeminiEvalModelGateway

from indusguard_api.main import create_app
from indusguard_api.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LOCAL_OWNER_TOKEN = "local-tractian-owner-token-with-at-least-thirty-two-chars"

os.environ.setdefault("TRACTIAN_API_URL", "http://localhost:8000")

settings = Settings(
    _env_file=None,
    environment="local-tractian-playground",
    execution_mode="simulate",
    connectors_dir=REPOSITORY_ROOT / "connectors",
    cors_allowed_origins=["http://127.0.0.1:3100", "http://localhost:3100"],
    database_url=os.environ.get(
        "INDUSGUARD_DATABASE_URL",
        "sqlite+aiosqlite:///./.data/tractian-playground.db",
    ),
    persist_runs=True,
    trace_jsonl_enabled=True,
    public_runs_enabled=True,
    public_connector_ids=["tractian"],
    public_run_owner_id=os.environ.get("INDUSGUARD_PUBLIC_RUN_OWNER_ID", "usr_sofia"),
    owner_token=os.environ.get("INDUSGUARD_OWNER_TOKEN", LOCAL_OWNER_TOKEN),
    public_run_rate_limit_per_hour=20,
)

app = create_app(settings=settings, public_model_gateway=GeminiEvalModelGateway())
