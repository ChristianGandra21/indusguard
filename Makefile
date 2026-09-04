.PHONY: setup dev-api dev-web dev-tractian-playground test test-web lint lint-web format validate eval-validate eval-pilot-fake migrate migration-check contracts e2e-api

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

# Ambiente manual do proprietário para testar Tractian no playground.
# Sobe fixture industrial em 8000, API em 8766 e web em 3100; Ctrl+C encerra os três.
dev-tractian-playground:
	mkdir -p .data
	INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/tractian-playground.db .venv/bin/alembic -c apps/api/alembic.ini upgrade head
	bash -c '\
		set -euo pipefail; \
		cleanup() { kill "$${fixture_pid:-}" "$${api_pid:-}" "$${web_pid:-}" 2>/dev/null || true; wait 2>/dev/null || true; }; \
		trap "cleanup" INT TERM EXIT; \
		.venv/bin/python -c "from pathlib import Path; import uvicorn; from indusguard_evals.tractian_fixture import store; store.configure_data_dir(Path(\"evals/corpus/official-v1/fixture/data\")); from indusguard_evals.tractian_fixture.main import app; uvicorn.run(app, host=\"127.0.0.1\", port=8000)" & \
		fixture_pid=$$!; \
		INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/tractian-playground.db TRACTIAN_API_URL=http://127.0.0.1:8000 .venv/bin/uvicorn tractian_playground_app:app --app-dir apps/api/scripts --host 127.0.0.1 --port 8766 & \
		api_pid=$$!; \
		NEXT_PUBLIC_INDUSGUARD_API_URL=http://127.0.0.1:8766 npm --prefix apps/web run dev -- --hostname 127.0.0.1 --port 3100 & \
		web_pid=$$!; \
		wait'

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
	INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:///./.data/e2e-dashboard.db .venv/bin/uvicorn e2e_app:app --app-dir apps/api/scripts --host 127.0.0.1 --port 8765
