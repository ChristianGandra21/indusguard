#!/bin/sh
set -eu

# Toda instância sobe no mesmo schema antes de aceitar tráfego. O comando é idempotente.
alembic -c /app/alembic.ini upgrade head

# Render fornece PORT; 10000 também torna a imagem executável localmente.
exec uvicorn indusguard_api.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-10000}" \
  --proxy-headers
