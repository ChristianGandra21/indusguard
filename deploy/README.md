# Deployment

O runtime já possui SQLAlchemy/Alembic, SQLite local, OpenTelemetry JSONL e OTLP opcional. Ainda
faltam os manifestos de container, Render, Neon e Grafana. O modo público continuará usando
`INDUSGUARD_EXECUTION_MODE=simulate`.

O objetivo desta pasta é concentrar Dockerfiles e configuração de infraestrutura sem misturar
detalhes de um provedor com o código da aplicação. Nenhum deployment público existe nesta etapa.
