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

## Estado atual

A fundação executável do backend está pronta. O FastAPI descobre conectores em `connectors/`,
valida os contratos e expõe saúde, versão e catálogo de operações. Nenhum LLM é necessário nesta
etapa e toda execução mutável permanece em modo de simulação por padrão.

```text
apps/
├── api/                  # FastAPI e catálogo genérico
└── web/                  # Next.js (próxima etapa)
connectors/
├── tractian/             # primeiro conector real do desafio
└── synthetic/            # prova de extensibilidade por configuração
deploy/                   # deployment e infraestrutura
evals/                    # avaliações isoladas do runtime
└── .github/workflows/    # gates de integração contínua
```

## Executar localmente

Requisitos: Python 3.12.

```bash
make setup
make validate
make test
make dev-api
```

A documentação interativa estará em `http://127.0.0.1:8000/docs` e os primeiros contratos são:

- `GET /api/v1/health`
- `GET /api/v1/ready`
- `GET /api/v1/version`
- `GET /api/v1/connectors`
- `GET /api/v1/connectors/{connector_id}/operations`

O arquivo `.env.example` documenta as configurações. URLs e credenciais são lidas do ambiente;
segredos não entram em prompts, perfis ou respostas da API.

## Segurança por padrão

- toda operação ausente do `profile.yaml` nasce desabilitada;
- o loader rejeita chaves YAML duplicadas, `$ref` externo e conteúdo não JSON;
- o arquivo OpenAPI deve permanecer dentro do diretório do conector;
- escrita declara risco, permissão, justificativa e confirmação explicitamente;
- o modo padrão é `simulate`, inclusive no deployment público.

O contrato Tractian fornecido repetia a chave `/assets/{assetId}` para GET e PATCH. A cópia do
conector une os dois métodos sob o mesmo path para impedir que parsers YAML descartem uma operação.

## Documentação do desafio

Os materiais fornecidos pela TRACTIAN serão mantidos isolados da aplicação. O gabarito de avaliação nunca será incluído no contexto ou na imagem de runtime do agente.
