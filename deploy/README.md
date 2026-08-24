# Deployment sem provisionamento automático

Esta pasta deixa o IndusGuard pronto para implantação, mas nenhum recurso externo é criado pelo
repositório ou pelos testes. A pessoa proprietária ainda precisa criar e vincular Render, Neon,
Groq e, opcionalmente, Grafana Cloud.

## O que entra na imagem

O `api.Dockerfile` é multi-stage. O primeiro estágio compila wheels; o segundo contém somente:

- pacote `indusguard-api` e dependências de runtime;
- migrações Alembic;
- conectores OpenAPI/YAML;
- script de inicialização.

Ele não copia `evals/`, frontend, testes, fixture industrial, Parquet, golden set, `.env` ou
`.data`. O processo roda como UID/GID `10001`, aplica `alembic upgrade head` e só então inicia o
Uvicorn na porta fornecida por `PORT`.

## Teste local da imagem

```bash
docker build -f deploy/api.Dockerfile -t indusguard-api:local .
docker run --rm -p 10000:10000 \
  -e INDUSGUARD_DATABASE_URL=sqlite+aiosqlite:////tmp/indusguard.db \
  -e INDUSGUARD_PUBLIC_RUNS_ENABLED=false \
  -e INDUSGUARD_TRACE_JSONL_ENABLED=false \
  indusguard-api:local
```

Em outro terminal:

```bash
curl -fsS http://localhost:10000/api/v1/ready
```

O `curl` deve retornar `status=ready`. A imagem declara o usuário não-root `10001:10001`.

## Neon

Copie a connection string atual do Neon para `INDUSGUARD_DATABASE_URL`. URLs em
`*.neon.tech` só são aceitas com:

```text
?sslmode=require&channel_binding=require
```

O backend seleciona Psycopg 3 assíncrono e preserva esses parâmetros libpq. Não cole a URL em
arquivo versionado, issue ou log.

## Render Blueprint

O `render.yaml` descreve dois serviços gratuitos:

- `indusguard-api`: Docker, modo `simulate`, JSONL desligado e deploy após CI verde;
- `indusguard-web`: exportação estática Next.js com Node 20.

Ao importar o Blueprint, preencha os valores `sync: false`:

1. `INDUSGUARD_DATABASE_URL`: URL Neon protegida por TLS;
2. `INDUSGUARD_OWNER_TOKEN`: token aleatório com pelo menos 32 caracteres;
3. `GROQ_API_KEY`: chave da faixa gratuita;
4. `INDUSGUARD_CORS_ALLOWED_ORIGINS`: JSON com a URL final do frontend;
5. `NEXT_PUBLIC_INDUSGUARD_API_URL`: URL pública final do backend.

O [Render Free](https://render.com/docs/free) suspende um backend após 15 minutos ocioso e a volta
pode levar cerca de um minuto. Seu filesystem também é efêmero; por isso o estado fica no Neon e
JSONL está desligado. O plano gratuito não deve ser tratado como SLA de produção.

## Grafana Cloud opcional

O produto funciona sem Grafana. O envio direto é suportado pela documentação
[Grafana Cloud OTLP](https://grafana.com/docs/opentelemetry/grafana-cloud/). Para exportar spans,
configure depois no Render:

```text
INDUSGUARD_OTLP_ENABLED=true
INDUSGUARD_OTLP_ENDPOINT=https://SEU-ENDPOINT-OTLP/v1/traces
INDUSGUARD_OTLP_HEADERS=Authorization=Basic VALOR_BASE64
```

Endpoint e header são secrets. Se o exporter falhar, a run funcional continua e a observabilidade
é marcada como degradada.

## Custo e limites

Os manifestos selecionam opções gratuitas. Na consulta de 24/08/2026, o Render oferece 750 horas
mensais e, sem método de pagamento, suspende serviços ou novos builds ao esgotar os limites em vez
de cobrar; o [Neon Free](https://neon.com/pricing) informa 100 CU-horas e 0,5 GB por projeto, sem
cartão. Esses termos podem mudar. Confira os painéis antes de importar o Blueprint e não adicione
método de pagamento se a sua garantia operacional for impedir cobrança. Este repositório não cria
recursos nem ativa upgrades.
