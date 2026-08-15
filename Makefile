.PHONY: setup dev-api test lint format validate

setup:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e "./apps/api[dev]"

dev-api:
	.venv/bin/uvicorn indusguard_api.main:app --app-dir apps/api/src --reload

test:
	.venv/bin/pytest apps/api

lint:
	.venv/bin/ruff check apps/api
	.venv/bin/ruff format --check apps/api

format:
	.venv/bin/ruff check --fix apps/api
	.venv/bin/ruff format apps/api

validate:
	.venv/bin/python -c "from indusguard_api.connectors import ConnectorCatalog; from pathlib import Path; c = ConnectorCatalog(Path('connectors')); c.load(); print(f'{len(c.list())} conectores válidos')"
