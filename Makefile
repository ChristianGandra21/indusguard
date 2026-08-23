.PHONY: setup dev-api test lint format validate eval-validate eval-pilot-fake migrate migration-check

# Os comandos chamam executáveis dentro de .venv diretamente; ativar o ambiente é opcional.
setup:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e "./apps/api[dev]"
	.venv/bin/python -m pip install -e "./evals[fixture,dev]"

# --app-dir permite manter o layout src/ sem instalar caminhos globais.
dev-api:
	.venv/bin/uvicorn indusguard_api.main:app --app-dir apps/api/src --reload

test:
	.venv/bin/pytest apps/api --cov=indusguard_api --cov-report=term-missing
	.venv/bin/pytest -c evals/pyproject.toml evals/tests --cov=indusguard_evals --cov-config=evals/pyproject.toml --cov-report=term-missing

lint:
	.venv/bin/ruff check apps/api
	.venv/bin/ruff check evals
	.venv/bin/ruff format --check apps/api evals

format:
	.venv/bin/ruff check --fix apps/api
	.venv/bin/ruff check --fix evals
	.venv/bin/ruff format apps/api evals

# Carregar o catálogo executa as mesmas validações usadas no startup do FastAPI.
validate:
	.venv/bin/python -c "from indusguard_api.connectors import ConnectorCatalog; from pathlib import Path; c = ConnectorCatalog(Path('connectors')); c.load(); print(f'{len(c.list())} conectores válidos')"

eval-validate:
	.venv/bin/indusguard-eval validate

# Smoke de infraestrutura: não chama Groq e não gera resultado científico.
eval-pilot-fake:
	.venv/bin/indusguard-eval pilot --fake

# Alembic lê INDUSGUARD_DATABASE_URL; sem override, usa o SQLite local em .data/.
migrate:
	.venv/bin/alembic -c apps/api/alembic.ini upgrade head

migration-check:
	.venv/bin/alembic -c apps/api/alembic.ini check
