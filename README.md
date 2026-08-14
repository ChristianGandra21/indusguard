# IndusGuard

Plataforma fullstack e orientada a avaliações para construir, observar e implantar agentes de IA conectados a APIs REST descritas por OpenAPI.

O primeiro conector será a API industrial sintética fornecida no desafio TRACTIAN × Inteli. A arquitetura será configurável por OpenAPI e perfis YAML para permitir a integração de outras APIs sem reescrever o núcleo do agente.

## Objetivos

- gerar tools tipadas a partir de contratos OpenAPI 3.x;
- aplicar políticas determinísticas antes de operações de escrita;
- comparar agentes `prompt_only` e `guarded` em cenários reproduzíveis;
- oferecer observabilidade ponta a ponta com traces e métricas;
- entregar uma aplicação fullstack implantável com custo zero.

## Stack planejada

- **Web:** Next.js, TypeScript, Tailwind CSS, TanStack Query e Recharts;
- **API e agente:** FastAPI, Pydantic, LangGraph, MCP e Groq;
- **Dados e observabilidade:** PostgreSQL/SQLite, SQLAlchemy, Alembic e OpenTelemetry;
- **Qualidade e entrega:** pytest, Playwright, Schemathesis, Docker e GitHub Actions.

## Estado

Repositório em fase inicial de estruturação. A implementação será feita incrementalmente, começando pelo núcleo genérico de conectores OpenAPI.

## Documentação do desafio

Os materiais fornecidos pela TRACTIAN serão mantidos isolados da aplicação. O gabarito de avaliação nunca será incluído no contexto ou na imagem de runtime do agente.

