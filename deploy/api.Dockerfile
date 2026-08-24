# syntax=docker/dockerfile:1

# O builder baixa dependências e produz wheels. Nada desse ambiente de compilação chega ao runtime.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY apps/api/pyproject.toml apps/api/README.md ./apps/api/
COPY apps/api/src ./apps/api/src
RUN python -m pip wheel --wheel-dir /wheels ./apps/api

# A imagem final recebe somente o pacote da API, migrações e perfis de conectores.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INDUSGUARD_CONNECTORS_DIR=/app/connectors \
    INDUSGUARD_EXECUTION_MODE=simulate

RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 indusguard \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin indusguard

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels indusguard-api==0.1.0 \
    && rm -rf /wheels

COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/migrations ./migrations
COPY connectors ./connectors
COPY deploy/start-api.sh ./start-api.sh
RUN chmod 0555 /app/start-api.sh \
    && chown -R indusguard:indusguard /app

USER 10001:10001
EXPOSE 10000

ENTRYPOINT ["/app/start-api.sh"]
