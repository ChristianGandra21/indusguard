.PHONY: setup dev-api dev-web test test-web lint lint-web format validate eval-validate eval-pilot-fake migrate migration-check contracts e2e-api

# Os comandos chamam executáveis dentro de .venv diretamente; ativar o ambiente é opcional.
setup:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e "./apps/api[dev]"
	.venv/bin/python -m pip install -e "./evals[fixture,dev]"
	npm --prefix apps/web install

# --app-dir permite manter o layout src/ sem instalar caminhos globais.
dev-api:
	.venv/bin/uvicorn indusguard_api.main:app --app-dir apps/api/src --reload

dev-web:
	npm --prefix apps/web run dev

test:
	.venv/bin/pytest apps/api --cov=indusguard_api --cov-report=term-missing
	.venv/bin/pytest -c evals/pyproject.toml evals/tests --cov=indusguard_evals --cov-config=evals/pyproject.toml --cov-report=term-missing

test-web:
	npm --prefix apps/web test
	npm --prefix apps/web run typecheck

lint:
	.venv/bin/ruff check apps/api
	.venv/bin/ruff check evals
	.venv/bin/ruff format --check apps/api evals

lint-web:
	npm --prefix apps/web run lint

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

# Gera o snapshot OpenAPI e os tipos consumidos pelo frontend.
contracts:
	.venv/bin/python apps/api/scripts/export_openapi.py apps/web/openapi/indusguard.openapi.json
	npm --prefix apps/web run api:generate

# Servidor local usado pelo Playwright. O seed é sintético e não importa o pacote evals.
e2e-api:
	mkdir -p .data
	INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/e2e-dashboard.db .venv/bin/alembic -c apps/api/alembic.ini upgrade head
	INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/e2e-dashboard.db .venv/bin/python apps/api/scripts/seed_dashboard_demo.py
	INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/e2e-dashboard.db INDUSGUARD_CORS_ALLOWED_ORIGINS='["http://127.0.0.1:3100"]' .venv/bin/uvicorn indusguard_api.main:app --app-dir apps/api/src --host 127.0.0.1 --port 8765
